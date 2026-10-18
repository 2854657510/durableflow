from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agent.memory import MemoryStore
from app.agent.tools import ToolRegistry
from app.core.context import ContextManager
from app.core.events import record_event
from app.core.retry import ApprovalRequired, NonRetryableError, RetryableError, ToolArgumentError
from app.llm.client import LLMClient, LLMResponse
from app.models import LlmCall, StepEffect, WorkflowRun


@dataclass
class AgentLoopResult:
    status: str  # completed / pending_approval / max_steps / error
    answer: str = ""
    steps: list[dict] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0
    pending_tool: str | None = None
    pending_arguments: dict | None = None
    error: str | None = None


class AgentLoop:
    """一个最小但完整的 ReAct 循环。

    它把“执行链路”拆成：模型决策 -> 工具调用 -> 观察回喂 -> 再决策。
    工具失败不抛穿整个步骤，而是作为 Observation 回给模型；高风险工具
    在权限层直接拦截，返回 pending_approval 交给工作流审批节点。
    """

    def __init__(
        self,
        llm: LLMClient,
        registry: ToolRegistry,
        memory: MemoryStore,
        context: ContextManager,
        *,
        max_steps: int = 8,
        namespace: str = "default",
    ):
        self.llm = llm
        self.registry = registry
        self.memory = memory
        self.context = context
        self.max_steps = max_steps
        self.namespace = namespace

    def _record_llm(
        self,
        session: Session,
        run_id: int,
        step_id: int,
        response: LLMResponse,
        prompt_version: str = "agent",
    ) -> None:
        session.add(
            LlmCall(
                run_id=run_id,
                step_id=step_id,
                model=response.model,
                prompt_version=prompt_version,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                cost=response.cost,
                latency_ms=response.latency_ms,
            )
        )
        run = session.get(WorkflowRun, run_id)
        if run is not None:
            run.total_tokens += response.prompt_tokens + response.completion_tokens
            run.total_cost += response.cost
            if run.max_cost is not None and run.total_cost > run.max_cost:
                raise NonRetryableError(
                    f"运行成本 {run.total_cost:.4f} 超过预算 {run.max_cost:.4f}"
                )
        session.flush()

    def _record_effect(self, session: Session, run_id: int, step_id: int, key: str, payload: dict) -> dict:
        full_key = f"{run_id}:{step_id}:{key}"
        try:
            with session.begin_nested():
                session.add(StepEffect(key=full_key, step_id=step_id, payload=payload))
                session.flush()
            return payload
        except IntegrityError:
            existing = session.scalar(select(StepEffect).where(StepEffect.key == full_key))
            return existing.payload if existing else payload

    def run(
        self,
        session: Session,
        *,
        run_id: int,
        step_id: int,
        goal: str,
        approved_tools: set[str] | None = None,
    ) -> AgentLoopResult:
        approved = set(approved_tools or [])
        tools = self.registry.specs_for_prompt()
        tool_lines = "\n".join(
            f"- {t['name']}（权限：{t['permission']}）：{t['description']}" for t in tools
        )
        memory_text = self.memory.context_for(session, self.namespace, goal, limit=5)
        system = (
            "你是一个企业级任务 Agent。你会收到一个目标，需要决定调用哪个工具。\n"
            "只输出 JSON：{\"thought\":\"...\",\"action\":{\"tool\":\"...\",\"arguments\":{...}} 或 null,"
            "\"final_answer\":\"...\" 或 null}。\n"
            "规则：\n"
            "1. 需要外部信息时先调工具，不要编造工具结果。\n"
            "2. 工具返回 Observation 后，再决定下一步或给最终答案。\n"
            "3. 高风险工具（dangerous）必须等待人工审批。\n"
            "4. 工具失败时读取错误信息，修正参数或换工具，不要重复同样的错误。\n"
            f"可用工具：\n{tool_lines}\n"
            f"长期记忆：\n{memory_text or '（暂无）'}"
        )
        messages: list[dict] = [
            {"role": "system", "content": system},
            {"role": "user", "content": f"目标：{goal}"},
        ]

        steps: list[dict] = []
        prompt_tokens = completion_tokens = 0
        cost = 0.0
        invalid_json = 0
        tool_failures: Counter[str] = Counter()

        for index in range(1, self.max_steps + 1):
            ctx = self.context.build(messages)
            if ctx.compressed:
                record_event(
                    session,
                    run_id,
                    "context_compressed",
                    step_id=step_id,
                    payload={
                        "original_tokens": ctx.original_tokens,
                        "final_tokens": ctx.final_tokens,
                        "kept_last_messages": self.context.keep_last_messages,
                    },
                )

            try:
                response = self.llm.chat(ctx.messages, response_format={"type": "json_object"})
            except NonRetryableError:
                raise
            prompt_tokens += response.prompt_tokens
            completion_tokens += response.completion_tokens
            cost += response.cost
            self._record_llm(session, run_id, step_id, response)

            try:
                data = json.loads(response.content)
                if not isinstance(data, dict):
                    raise ValueError("响应不是 JSON 对象")
            except (json.JSONDecodeError, ValueError) as exc:
                invalid_json += 1
                messages.append({"role": "assistant", "content": response.content})
                messages.append(
                    {
                        "role": "user",
                        "content": f"输出不是合法 JSON（{exc}）。请只输出规定格式的 JSON。",
                    }
                )
                if invalid_json >= 2:
                    return AgentLoopResult(
                        status="error",
                        steps=steps,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        cost=cost,
                        error="模型连续两次输出非法 JSON",
                    )
                continue

            thought = str(data.get("thought", ""))
            action = data.get("action")
            final_answer = data.get("final_answer")

            if not action:
                answer = str(final_answer or thought or "模型没有返回可执行动作")
                steps.append(
                    {
                        "index": index,
                        "thought": thought,
                        "tool": None,
                        "arguments": {},
                        "observation": None,
                        "status": "final",
                    }
                )
                self.memory.write(
                    session,
                    self.namespace,
                    f"run:{run_id}",
                    answer,
                    tags=["agent", "result"],
                    importance=6,
                )
                return AgentLoopResult(
                    status="completed",
                    answer=answer,
                    steps=steps,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cost=cost,
                )

            tool_name = action.get("tool") if isinstance(action, dict) else None
            arguments = action.get("arguments", {}) if isinstance(action, dict) else {}
            if not isinstance(arguments, dict):
                arguments = {}

            spec = self.registry.get(tool_name or "")
            if spec is None:
                observation = {"error": f"工具不存在: {tool_name}", "available": list(self.registry.tools)}
                steps.append(
                    {
                        "index": index,
                        "thought": thought,
                        "tool": tool_name,
                        "arguments": arguments,
                        "observation": observation,
                        "status": "tool_not_found",
                    }
                )
                messages.append({"role": "assistant", "content": json.dumps(data, ensure_ascii=False)})
                messages.append({"role": "user", "content": f"Observation: {json.dumps(observation, ensure_ascii=False)}"})
                continue

            if spec.permission == "dangerous" and tool_name not in approved:
                return AgentLoopResult(
                    status="pending_approval",
                    steps=steps,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cost=cost,
                    pending_tool=tool_name,
                    pending_arguments=arguments,
                )

            try:
                observation = self.registry.execute(
                    tool_name,
                    arguments,
                    approved=tool_name in approved,
                    context={
                        "run_id": run_id,
                        "step_id": step_id,
                        "namespace": self.namespace,
                        "record_effect": lambda k, p: self._record_effect(session, run_id, step_id, k, p),
                    },
                )
                status = "success"
            except ApprovalRequired as exc:
                return AgentLoopResult(
                    status="pending_approval",
                    steps=steps,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cost=cost,
                    pending_tool=exc.tool,
                    pending_arguments=exc.arguments,
                )
            except ToolArgumentError as exc:
                observation = {"error": str(exc), "hint": "参数不合法，请修正后重试"}
                status = "tool_argument_error"
            except RetryableError as exc:
                tool_failures[tool_name] += 1
                observation = {"error": str(exc), "hint": "工具暂时失败，可以重试或换工具"}
                status = "tool_error"
                if tool_failures[tool_name] >= 3:
                    return AgentLoopResult(
                        status="error",
                        steps=steps,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        cost=cost,
                        error=f"工具 {tool_name} 连续失败 3 次: {exc}",
                    )

            steps.append(
                {
                    "index": index,
                    "thought": thought,
                    "tool": tool_name,
                    "arguments": arguments,
                    "observation": observation,
                    "status": status,
                }
            )
            messages.append({"role": "assistant", "content": json.dumps(data, ensure_ascii=False)})
            messages.append(
                {"role": "user", "content": f"Observation: {json.dumps(observation, ensure_ascii=False)}"}
            )

        return AgentLoopResult(
            status="max_steps",
            answer=f"已达到最大步数 {self.max_steps}，任务未收敛，建议拆分目标或人工介入。",
            steps=steps,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost=cost,
        )
