from __future__ import annotations

from sqlalchemy import select

from app.core.events import record_event
from app.core.retry import NonRetryableError
from app.models import Approval
from app.nodes.base import NodeContext, NodeResult


def execute_approval_node(ctx: NodeContext) -> NodeResult:
    approval = ctx.session.scalar(select(Approval).where(Approval.step_id == ctx.step.id))
    if approval is None:
        approval = Approval(step_id=ctx.step.id, status="PENDING")
        ctx.session.add(approval)
        ctx.session.flush()
        record_event(
            ctx.session,
            ctx.run.id,
            "approval_requested",
            step_id=ctx.step.id,
            payload={"approval_id": approval.id},
        )
        return NodeResult(status="waiting_approval", output={"approval_id": approval.id})

    if approval.status == "APPROVED":
        return NodeResult(
            output={
                "approved": True,
                "approval_id": approval.id,
                "decided_by": approval.decided_by,
                "comment": approval.comment,
            }
        )
    if approval.status == "REJECTED":
        raise NonRetryableError("人工审批拒绝")
    return NodeResult(status="waiting_approval", output={"approval_id": approval.id})
