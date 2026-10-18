from __future__ import annotations

import json
from dataclasses import dataclass

from app.agent.tools import ToolRegistry
from app.llm.client import LLMClient, LLMResponse


@dataclass
class PlanStep:
    name: str
    goal: str
    tool: str | None = None


@dataclass
class Plan:
    reasoning: str
    steps: list[PlanStep]


@dataclass
class PlannedResult:
    plan: Plan
    llm: LLMResponse | None = None
    fallback: bool = False


class Planner:
    """把目标拆成子步骤。

    真实模型走 JSON 规划；解析失败时退回规则规划，保证任务还能继续。
    """

    def plan(self, goal: str, llm: LLMClient, tools: ToolRegistry) -> PlannedResult:
        tool_lines = "\n".join(
            f"- {t['name']}({t['permission']}): {t['description']}" for t in tools.specs_for_prompt()
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "你是任务规划器。把目标拆成 2-4 个可执行子步骤。"
                    "只输出 JSON：{\"reasoning\":\"...\",\"steps\":[{\"name\":\"...\",\"goal\":\"...\",\"tool\":\"工具名或 null\"}]}。"
                    f"\n可用工具：\n{tool_lines}"
                ),
            },
            {"role": "user", "content": f"目标：{goal}"},
        ]
        response = llm.chat(messages, response_format={"type": "json_object"})
        try:
            data = json.loads(response.content)
            steps = [
                PlanStep(
                    name=str(item.get("name", f"step-{i + 1}")),
                    goal=str(item.get("goal", "")),
                    tool=item.get("tool"),
                )
                for i, item in enumerate(data.get("steps", []))
                if isinstance(item, dict)
            ]
            if steps:
                return PlannedResult(
                    plan=Plan(reasoning=str(data.get("reasoning", "")), steps=steps),
                    llm=response,
                )
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass

        # 兜底：即使规划模型输出坏了，任务也能按固定两步继续。
        return PlannedResult(
            plan=Plan(
                reasoning="模型规划输出不可解析，使用规则兜底。",
                steps=[
                    PlanStep(name="search", goal="检索与目标相关的资料", tool="search_docs"),
                    PlanStep(name="summarize", goal="总结检索结果并给出结论"),
                ],
            ),
            llm=response,
            fallback=True,
        )
