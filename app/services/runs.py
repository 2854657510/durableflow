from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core.clock import utcnow
from app.core.events import record_event
from app.core.outbox import enqueue
from app.models import Approval, Event, LlmCall, StepRun, WorkflowDefinition, WorkflowRun
from app.observability import metrics
from app.state_machine import (
    RunStatus,
    StepStatus,
    TERMINAL_RUN_STATUSES,
    ensure_run_transition,
)


class RunError(ValueError):
    pass


def get_run(session: Session, run_id: int) -> WorkflowRun | None:
    return session.get(WorkflowRun, run_id)


def get_run_with_steps(session: Session, run_id: int) -> WorkflowRun | None:
    return session.scalar(
        select(WorkflowRun)
        .where(WorkflowRun.id == run_id)
        .options(selectinload(WorkflowRun.steps), selectinload(WorkflowRun.workflow))
    )


def list_runs(
    session: Session,
    limit: int = 50,
    status: str | None = None,
    offset: int = 0,
) -> list[WorkflowRun]:
    stmt = select(WorkflowRun).order_by(WorkflowRun.id.desc()).offset(offset).limit(limit)
    if status:
        stmt = stmt.where(WorkflowRun.status == status)
    return list(session.scalars(stmt))


def count_runs(session: Session, status: str | None = None) -> int:
    stmt = select(func.count()).select_from(WorkflowRun)
    if status:
        stmt = stmt.where(WorkflowRun.status == status)
    return int(session.scalar(stmt) or 0)


def create_run(
    session: Session,
    workflow: WorkflowDefinition,
    input_data: dict | None = None,
    *,
    idempotency_key: str | None = None,
    max_cost: float | None = None,
) -> tuple[WorkflowRun, bool]:
    input_data = input_data or {}
    if idempotency_key:
        existing = session.scalar(
            select(WorkflowRun).where(WorkflowRun.idempotency_key == idempotency_key)
        )
        if existing is not None:
            return existing, False

    run = WorkflowRun(
        workflow_id=workflow.id,
        status=RunStatus.PENDING,
        input=input_data,
        idempotency_key=idempotency_key,
        max_cost=float(max_cost if max_cost is not None else 1.0),
    )
    session.add(run)
    session.flush()

    steps = workflow.spec.get("steps", [])
    for position, step in enumerate(steps):
        deps = list(step.get("depends_on", []))
        step_run = StepRun(
            run_id=run.id,
            step_name=step["name"],
            node_type=step.get("type", "function"),
            config=step,
            depends_on=deps,
            position=position,
            status=StepStatus.PENDING if not deps else StepStatus.BLOCKED,
            max_attempts=int(step.get("max_attempts", 3)),
            available_at=utcnow(),
        )
        session.add(step_run)

    if not steps:
        run.status = RunStatus.SUCCEEDED
        run.started_at = utcnow()
        run.finished_at = utcnow()

    record_event(session, run.id, "run_created", payload={"workflow": workflow.name, "input": input_data})
    enqueue(session, "run.created", {"run_id": run.id, "workflow": workflow.name})

    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        if idempotency_key:
            existing = session.scalar(
                select(WorkflowRun).where(WorkflowRun.idempotency_key == idempotency_key)
            )
            if existing is not None:
                return existing, False
        raise
    session.refresh(run)
    return run, True


def release_ready_steps(session: Session, run_id: int) -> list[StepRun]:
    """把依赖已全部成功的 BLOCKED 步骤释放为 PENDING。"""
    steps = list(session.scalars(select(StepRun).where(StepRun.run_id == run_id)))
    status_by_name = {s.step_name: s.status for s in steps}
    released: list[StepRun] = []
    for step in steps:
        if step.status != StepStatus.BLOCKED:
            continue
        if all(status_by_name.get(dep) == StepStatus.SUCCEEDED for dep in step.depends_on):
            step.status = StepStatus.PENDING
            step.available_at = utcnow()
            record_event(session, run_id, "step_released", step_id=step.id, payload={"step": step.step_name})
            released.append(step)
    return released


def refresh_run_status(session: Session, run: WorkflowRun) -> str:
    """根据步骤状态推导运行状态。调用方负责 commit。"""
    if run.status == RunStatus.CANCELED:
        return run.status

    steps = list(session.scalars(select(StepRun).where(StepRun.run_id == run.id)))
    old = run.status
    if not steps:
        new = RunStatus.SUCCEEDED
    elif any(s.status in {StepStatus.FAILED, StepStatus.DEAD} for s in steps):
        new = RunStatus.FAILED
        failed = next(s for s in steps if s.status in {StepStatus.FAILED, StepStatus.DEAD})
        run.error = failed.error or f"step {failed.step_name} failed"
    elif any(s.status == StepStatus.WAITING_APPROVAL for s in steps):
        new = RunStatus.WAITING_APPROVAL
    elif all(s.status == StepStatus.SUCCEEDED for s in steps):
        new = RunStatus.SUCCEEDED
        run.finished_at = utcnow()
        last = max(steps, key=lambda s: s.position)
        run.output = last.output or {}
    elif any(s.status in {StepStatus.RUNNING, StepStatus.RETRYING, StepStatus.PENDING} for s in steps):
        new = RunStatus.RUNNING
    else:
        new = RunStatus.PENDING if old == RunStatus.PENDING else RunStatus.RUNNING

    if new != old:
        ensure_run_transition(old, new)
        run.status = new
        if new == RunStatus.RUNNING and run.started_at is None:
            run.started_at = utcnow()
        if new in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELED}:
            run.finished_at = run.finished_at or utcnow()
            metrics.inc_run(new)
            enqueue(
                session,
                "run.finished",
                {"run_id": run.id, "status": new, "error": run.error},
            )
    return new


def cancel_run(session: Session, run_id: int) -> WorkflowRun:
    run = session.get(WorkflowRun, run_id)
    if run is None:
        raise RunError("run 不存在")
    if run.status in TERMINAL_RUN_STATUSES:
        return run
    run.status = RunStatus.CANCELED
    run.finished_at = utcnow()
    for step in session.scalars(select(StepRun).where(StepRun.run_id == run_id)):
        if step.status in {StepStatus.PENDING, StepStatus.BLOCKED, StepStatus.RETRYING, StepStatus.WAITING_APPROVAL}:
            step.status = StepStatus.CANCELED
            step.finished_at = utcnow()
    record_event(session, run_id, "run_canceled")
    enqueue(session, "run.finished", {"run_id": run_id, "status": RunStatus.CANCELED})
    session.commit()
    return run


def retry_step(session: Session, step_id: int) -> StepRun:
    step = session.get(StepRun, step_id)
    if step is None:
        raise RunError("step 不存在")
    if step.status not in {StepStatus.FAILED, StepStatus.DEAD}:
        raise RunError(f"只有 FAILED/DEAD 步骤可以手动重试，当前为 {step.status}")

    approval = session.scalar(select(Approval).where(Approval.step_id == step.id))
    if approval is not None:
        session.delete(approval)

    step.status = StepStatus.PENDING
    step.attempt = 0
    step.error = None
    step.available_at = utcnow()
    step.lease_owner = None
    step.lease_expires_at = None
    step.heartbeat_at = None
    step.finished_at = None
    run = session.get(WorkflowRun, step.run_id)
    if run is not None:
        run.status = RunStatus.RUNNING
        run.error = None
        run.finished_at = None
    record_event(session, step.run_id, "step_manual_retry", step_id=step.id, payload={"step": step.step_name})
    session.commit()
    session.refresh(step)
    return step


def decide_approval(
    session: Session,
    approval_id: int,
    *,
    approved: bool,
    decided_by: str = "system",
    comment: str | None = None,
) -> Approval:
    approval = session.get(Approval, approval_id)
    if approval is None:
        raise RunError("approval 不存在")
    if approval.status != "PENDING":
        raise RunError(f"approval 已经处理过: {approval.status}")

    approval.status = "APPROVED" if approved else "REJECTED"
    approval.decided_at = utcnow()
    approval.decided_by = decided_by
    approval.comment = comment
    step = session.get(StepRun, approval.step_id)
    if step is None:
        raise RunError("approval 对应的 step 不存在")
    run = session.get(WorkflowRun, step.run_id)

    if approved:
        step.status = StepStatus.PENDING
        step.available_at = utcnow()
        step.lease_owner = None
        step.lease_expires_at = None
        step.error = None
        if run is not None:
            run.status = RunStatus.RUNNING
        event_type = "approval_approved"
    else:
        step.status = StepStatus.FAILED
        step.error = "人工审批拒绝"
        step.finished_at = utcnow()
        if run is not None:
            run.status = RunStatus.FAILED
            run.error = step.error
            run.finished_at = utcnow()
        event_type = "approval_rejected"

    record_event(
        session,
        step.run_id,
        event_type,
        step_id=step.id,
        payload={"approval_id": approval.id, "decided_by": decided_by, "comment": comment},
    )
    enqueue(
        session,
        event_type,
        {"run_id": step.run_id, "step_id": step.id, "approval_id": approval.id},
    )
    session.commit()
    session.refresh(approval)
    return approval


def get_cost_breakdown(session: Session, run_id: int) -> dict:
    calls = list(session.scalars(select(LlmCall).where(LlmCall.run_id == run_id)))
    by_model: dict[str, dict] = {}
    by_step: dict[int, dict] = {}
    for call in calls:
        model = by_model.setdefault(
            call.model,
            {"model": call.model, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0, "calls": 0},
        )
        model["prompt_tokens"] += call.prompt_tokens
        model["completion_tokens"] += call.completion_tokens
        model["cost"] += call.cost
        model["calls"] += 1

        step = by_step.setdefault(
            call.step_id,
            {"step_id": call.step_id, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0, "calls": 0},
        )
        step["prompt_tokens"] += call.prompt_tokens
        step["completion_tokens"] += call.completion_tokens
        step["cost"] += call.cost
        step["calls"] += 1

    return {
        "run_id": run_id,
        "total_tokens": sum(c.prompt_tokens + c.completion_tokens for c in calls),
        "total_cost": round(sum(c.cost for c in calls), 6),
        "by_model": list(by_model.values()),
        "by_step": list(by_step.values()),
    }


def get_events(session: Session, run_id: int, after_id: int = 0) -> list[Event]:
    return list(
        session.scalars(
            select(Event)
            .where(Event.run_id == run_id, Event.id > after_id)
            .order_by(Event.id)
        )
    )
