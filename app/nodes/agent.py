from __future__ import annotations

from sqlalchemy import select

from app.agent.loop import AgentLoop
from app.core.context import ContextManager
from app.core.retry import ApprovalRequired, NonRetryableError
from app.models import Approval
from app.nodes.base import NodeContext, NodeResult


def execute_agent_node(ctx: NodeContext) -> NodeResult:
    params = ctx.step.config.get("params", {})
    goal = ctx.run.input.get("goal") or params.get("goal") or "完成任务"
    namespace = params.get("namespace", "default")
    approved = set(ctx.approved_tools) | set(params.get("approved_tools", []))

    # 如果同一个步骤之前触发过高风险工具审批，审批通过后把工具放行。
    approval = ctx.session.scalar(
        select(Approval).where(
            Approval.step_id == ctx.step.id,
            Approval.status == "APPROVED",
        )
    )
    if approval is not None and approval.tool_name:
        approved.add(approval.tool_name)

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

    return NodeResult(
        output={
            "answer": result.answer,
            "steps": result.steps,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "cost": result.cost,
            "status": result.status,
        }
    )
