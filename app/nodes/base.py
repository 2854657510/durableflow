from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from jinja2 import StrictUndefined, Template
from sqlalchemy.orm import Session

from app.agent.memory import MemoryStore
from app.agent.tools import ToolRegistry
from app.config import Settings
from app.core.retry import NonRetryableError
from app.llm.client import LLMClient, LLMResponse
from app.models import LlmCall, StepRun, WorkflowRun
from app.observability import metrics


@dataclass
class NodeContext:
    session: Session
    run: WorkflowRun
    step: StepRun
    previous: dict[str, dict]
    settings: Settings
    llm: LLMClient
    registry: ToolRegistry
    memory: MemoryStore
    approved_tools: set[str] = field(default_factory=set)


@dataclass
class NodeResult:
    output: dict
    status: str = "success"  # success / waiting_approval


def render_params(params: dict, ctx: NodeContext) -> dict:
    """把 params 里的字符串按 Jinja2 渲染，支持 {{ input.xxx }}、{{ previous.step.output }}。"""
    rendered: dict[str, Any] = {}
    for key, value in (params or {}).items():
        if isinstance(value, str):
            rendered[key] = Template(value, undefined=StrictUndefined).render(
                input=ctx.run.input,
                previous=ctx.previous,
                run=ctx.run,
            )
        else:
            rendered[key] = value
    return rendered


def record_llm_call(
    ctx: NodeContext,
    response: LLMResponse,
    *,
    prompt_version: str = "v1",
) -> None:
    ctx.session.add(
        LlmCall(
            run_id=ctx.run.id,
            step_id=ctx.step.id,
            model=response.model,
            prompt_version=prompt_version,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            cost=response.cost,
            latency_ms=response.latency_ms,
        )
    )
    ctx.run.total_tokens += response.prompt_tokens + response.completion_tokens
    ctx.run.total_cost += response.cost
    metrics.observe_llm(
        response.model,
        response.prompt_tokens,
        response.completion_tokens,
        response.cost,
    )
    if ctx.run.max_cost is not None and ctx.run.total_cost > ctx.run.max_cost:
        raise NonRetryableError(
            f"运行成本 {ctx.run.total_cost:.4f} 超过预算 {ctx.run.max_cost:.4f}"
        )
    ctx.session.flush()
