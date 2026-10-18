from __future__ import annotations

import time
from typing import Callable

from app.core.context import ContextManager
from app.agent.loop import AgentLoop
from app.agent.planner import Planner
from app.core.outbox import enqueue
from app.core.retry import ApprovalRequired, NonRetryableError, RetryableError
from app.nodes.base import NodeContext, record_llm_call, render_params

Function = Callable[[dict, NodeContext], dict]


def echo(params: dict, ctx: NodeContext) -> dict:
    return {"message": params.get("message", ""), "run_id": ctx.run.id}


def add(params: dict, ctx: NodeContext) -> dict:
    return {"result": float(params.get("a", 0)) + float(params.get("b", 0))}


def sleep(params: dict, ctx: NodeContext) -> dict:
    seconds = float(params.get("seconds", 0.1))
    time.sleep(seconds)
    return {"slept_seconds": seconds}


def flaky(params: dict, ctx: NodeContext) -> dict:
    """前两次失败，第三次成功，用来演示重试和退避。"""
    if ctx.step.attempt < 2:
        raise RetryableError(f"flaky failure at attempt {ctx.step.attempt}")
    return {"attempt": ctx.step.attempt, "ok": True}


def write_memory(params: dict, ctx: NodeContext) -> dict:
    row = ctx.memory.write(
        ctx.session,
        params.get("namespace", "default"),
        params["key"],
        params["content"],
        tags=params.get("tags", []),
        importance=int(params.get("importance", 5)),
        ttl_seconds=params.get("ttl_seconds"),
    )
    return {"namespace": row.namespace, "key": row.key, "importance": row.importance}


def search_memory(params: dict, ctx: NodeContext) -> dict:
    rows = ctx.memory.search(
        ctx.session,
        params.get("namespace", "default"),
        params.get("query", ""),
        int(params.get("limit", 10)),
    )
    return {
        "results": [
            {"key": r.key, "content": r.content, "importance": r.importance}
            for r in rows
        ]
    }


def compact_memory(params: dict, ctx: NodeContext) -> dict:
    return ctx.memory.compact(
        ctx.session,
        params.get("namespace", "default"),
        max_items=int(params.get("max_items", ctx.settings.memory_compact_max_items)),
    )


def plan_task(params: dict, ctx: NodeContext) -> dict:
    goal = ctx.run.input.get("goal") or params.get("goal") or "完成目标"
    result = Planner().plan(goal, ctx.llm, ctx.registry)
    if result.llm is not None:
        record_llm_call(ctx, result.llm, prompt_version="planner")
    return {
        "reasoning": result.plan.reasoning,
        "steps": [
            {"name": s.name, "goal": s.goal, "tool": s.tool}
            for s in result.plan.steps
        ],
        "fallback": result.fallback,
    }


def run_agent(params: dict, ctx: NodeContext) -> dict:
    goal = ctx.run.input.get("goal") or params.get("goal") or "完成任务"
    namespace = params.get("namespace", "default")
    approved = set(ctx.approved_tools) | set(params.get("approved_tools", []))
    context = ContextManager(
        max_tokens=ctx.settings.context_max_tokens,
        keep_last_messages=ctx.settings.context_keep_last_messages,
    )
    loop = AgentLoop(
        ctx.llm,
        ctx.registry,
        ctx.memory,
        context,
        max_steps=int(params.get("max_steps", 8)),
        namespace=namespace,
    )
    result = loop.run(
        ctx.session,
        run_id=ctx.run.id,
        step_id=ctx.step.id,
        goal=goal,
        approved_tools=approved,
    )
    if result.status == "pending_approval":
        raise ApprovalRequired(
            result.pending_tool or "unknown",
            result.pending_arguments or {},
            reason="agent requested a dangerous tool",
        )
    if result.status == "error":
        raise NonRetryableError(result.error or "agent error")
    if result.status == "max_steps":
        raise NonRetryableError(result.answer)

    return {
        "answer": result.answer,
        "steps": result.steps,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "cost": result.cost,
        "status": result.status,
    }


def send_report(params: dict, ctx: NodeContext) -> dict:
    namespace = params.get("namespace", "default")
    execute_output = ctx.previous.get("execute", {}).get("output", {}) or {}
    review_output = ctx.previous.get("review", {}).get("output", {}) or {}
    review_data = review_output.get("data", review_output)
    content = (
        f"运行 {ctx.run.id} 完成。\n"
        f"Agent 回答：{execute_output.get('answer', '')}\n"
        f"审查结果：{review_data}"
    )
    ctx.memory.write(
        ctx.session,
        namespace,
        f"report:{ctx.run.id}",
        content,
        tags=["report", "run"],
        importance=7,
    )
    enqueue(ctx.session, "report.ready", {"run_id": ctx.run.id, "content": content})
    return {"report": content}


BUILTIN_FUNCTIONS: dict[str, Function] = {
    "echo": echo,
    "add": add,
    "sleep": sleep,
    "flaky": flaky,
    "write_memory": write_memory,
    "search_memory": search_memory,
    "compact_memory": compact_memory,
    "plan_task": plan_task,
    "run_agent": run_agent,
    "send_report": send_report,
}


def execute_function(name: str, params: dict, ctx: NodeContext) -> dict:
    fn = BUILTIN_FUNCTIONS.get(name)
    if fn is None:
        raise NonRetryableError(f"未知函数节点: {name}")
    rendered = render_params(params, ctx)
    result = fn(rendered, ctx)
    return result if isinstance(result, dict) else {"result": result}
