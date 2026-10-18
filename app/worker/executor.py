from __future__ import annotations

import logging
import time
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.memory import MemoryStore
from app.agent.tools import default_registry
from app.config import get_settings
from app.core.clock import utcnow
from app.core.events import record_event
from app.core.outbox import enqueue
from app.core.retry import (
    ApprovalRequired,
    RetryPolicy,
    compute_delay,
    is_retryable,
)
from app.db import SessionLocal
from app.llm.client import get_llm_client
from app.models import Approval, StepRun, WorkflowRun
from app.nodes.agent import execute_agent_node
from app.nodes.approval import execute_approval_node
from app.nodes.base import NodeContext, NodeResult
from app.nodes.builtin import execute_function
from app.nodes.llm import execute_llm_node
from app.observability import metrics
from app.observability.logging import set_context
from app.observability.tracing import span
from app.services.runs import refresh_run_status, release_ready_steps
from app.state_machine import RunStatus, StepStatus

logger = logging.getLogger(__name__)


def _previous_outputs(session: Session, run_id: int) -> dict[str, dict]:
    steps = list(session.scalars(select(StepRun).where(StepRun.run_id == run_id).order_by(StepRun.position)))
    return {
        step.step_name: {"status": step.status, "output": step.output or {}}
        for step in steps
    }


def _approved_tools_for_step(session: Session, step_id: int) -> set[str]:
    approval = session.scalar(
        select(Approval).where(
            Approval.step_id == step_id,
            Approval.status == "APPROVED",
        )
    )
    if approval is not None and approval.tool_name:
        return {approval.tool_name}
    return set()


def _request_approval(session: Session, step: StepRun, tool: str | None, arguments: dict) -> Approval:
    approval = session.scalar(select(Approval).where(Approval.step_id == step.id))
    if approval is None:
        approval = Approval(
            step_id=step.id,
            status="PENDING",
            tool_name=tool,
            arguments=arguments or {},
        )
        session.add(approval)
        session.flush()
        record_event(
            session,
            step.run_id,
            "approval_requested",
            step_id=step.id,
            payload={"approval_id": approval.id, "tool": tool, "arguments": arguments},
        )
    else:
        approval.tool_name = tool or approval.tool_name
        approval.arguments = arguments or approval.arguments
    return approval


def _dispatch(session: Session, run: WorkflowRun, step: StepRun, previous: dict[str, dict]) -> NodeResult:
    settings = get_settings()
    ctx = NodeContext(
        session=session,
        run=run,
        step=step,
        previous=previous,
        settings=settings,
        llm=get_llm_client(settings),
        registry=default_registry(),
        memory=MemoryStore(),
        approved_tools=_approved_tools_for_step(session, step.id),
    )

    node_type = step.node_type
    if node_type == "function":
        function_name = step.config.get("function", "")
        params = step.config.get("params", {})
        return NodeResult(output=execute_function(function_name, params, ctx))
    if node_type == "llm":
        return execute_llm_node(ctx)
    if node_type == "approval":
        return execute_approval_node(ctx)
    if node_type == "agent":
        return execute_agent_node(ctx)
    if node_type == "noop":
        return NodeResult(output={"ok": True})
    raise ValueError(f"不支持的节点类型: {node_type}")


def execute_step(step_id: int, worker_id: str) -> None:
    """执行一个已经 claim 的步骤。所有状态更新都在这里收口。"""
    settings = get_settings()
    started = time.perf_counter()
    with SessionLocal() as session:
        step = session.get(StepRun, step_id)
        if step is None:
            return
        run = session.get(WorkflowRun, step.run_id)
        if run is None:
            return
        set_context(run_id=run.id, step_id=step.id)
        logger.info(
            "step.start worker=%s step=%s attempt=%s node_type=%s",
            worker_id,
            step.step_name,
            step.attempt,
            step.node_type,
        )

        # 取消是协作式的：Worker 在步骤边界检查，不强行杀线程。
        if run.status == RunStatus.CANCELED:
            step.status = StepStatus.CANCELED
            step.finished_at = utcnow()
            step.lease_owner = None
            step.lease_expires_at = None
            session.commit()
            return

        run.status = RunStatus.RUNNING
        if run.started_at is None:
            run.started_at = utcnow()
        # 先提交 RUNNING，避免长事务；执行期间取消 API 才能看到状态变化。
        session.commit()
        previous = _previous_outputs(session, run.id)

        try:
            with span("step.execute", {"run_id": run.id, "step_id": step.id, "step": step.step_name}):
                result = _dispatch(session, run, step, previous)
        except ApprovalRequired as exc:
            _request_approval(session, step, exc.tool, exc.arguments)
            step.status = StepStatus.WAITING_APPROVAL
            step.lease_owner = None
            step.lease_expires_at = None
            step.error = None
            run.status = RunStatus.WAITING_APPROVAL
            session.commit()
            metrics.inc_step(StepStatus.WAITING_APPROVAL)
            logger.info("step.waiting_approval step=%s tool=%s", step.step_name, exc.tool)
            return
        except Exception as exc:  # noqa: BLE001 - 这里必须兜住所有异常
            _handle_failure(session, run, step, exc, started)
            return

        if result.status == "waiting_approval":
            _request_approval(session, step, None, {})
            step.status = StepStatus.WAITING_APPROVAL
            step.lease_owner = None
            step.lease_expires_at = None
            run.status = RunStatus.WAITING_APPROVAL
            session.commit()
            metrics.inc_step(StepStatus.WAITING_APPROVAL)
            logger.info("step.waiting_approval step=%s", step.step_name)
            return

        # 执行期间可能被取消。这里用标量查询，避免 refresh 覆盖内存里的 RUNNING。
        current_status = session.scalar(
            select(WorkflowRun.status).where(WorkflowRun.id == run.id)
        )
        if current_status == RunStatus.CANCELED:
            step.status = StepStatus.CANCELED
            step.finished_at = utcnow()
            step.lease_owner = None
            step.lease_expires_at = None
            session.commit()
            return

        step.status = StepStatus.SUCCEEDED
        step.output = result.output or {}
        step.error = None
        step.finished_at = utcnow()
        step.lease_owner = None
        step.lease_expires_at = None
        record_event(
            session,
            run.id,
            "step_succeeded",
            step_id=step.id,
            payload={"step": step.step_name, "attempt": step.attempt},
        )
        enqueue(
            session,
            "step.succeeded",
            {"run_id": run.id, "step_id": step.id, "step": step.step_name},
        )
        release_ready_steps(session, run.id)
        refresh_run_status(session, run)
        session.commit()

        duration = time.perf_counter() - started
        metrics.inc_step(StepStatus.SUCCEEDED)
        metrics.observe_step(step.node_type, StepStatus.SUCCEEDED, duration)
        logger.info("step.succeeded step=%s duration=%.3fs", step.step_name, duration)


def _handle_failure(
    session: Session,
    run: WorkflowRun,
    step: StepRun,
    exc: BaseException,
    started: float,
) -> None:
    policy = RetryPolicy.from_config(step.config.get("retry"), step.max_attempts)
    retryable = is_retryable(exc, policy)
    error = f"{type(exc).__name__}: {exc}"

    if retryable and step.attempt < step.max_attempts:
        delay = compute_delay(step.attempt, policy)
        step.status = StepStatus.RETRYING
        step.available_at = utcnow() + timedelta(seconds=delay)
        step.error = error
        step.lease_owner = None
        step.lease_expires_at = None
        run.status = RunStatus.RUNNING
        record_event(
            session,
            run.id,
            "step_retrying",
            step_id=step.id,
            payload={
                "step": step.step_name,
                "attempt": step.attempt,
                "max_attempts": step.max_attempts,
                "delay_s": round(delay, 3),
                "error": error,
            },
        )
        session.commit()
        metrics.inc_retry(type(exc).__name__)
        metrics.inc_step(StepStatus.RETRYING)
        logger.warning(
            "step.retrying step=%s attempt=%s/%s delay=%.3fs error=%s",
            step.step_name,
            step.attempt,
            step.max_attempts,
            delay,
            error,
        )
        return

    final_status = StepStatus.DEAD if retryable else StepStatus.FAILED
    step.status = final_status
    step.error = error
    step.finished_at = utcnow()
    step.lease_owner = None
    step.lease_expires_at = None
    run.status = RunStatus.FAILED
    run.error = f"step {step.step_name} {final_status}: {error}"
    run.finished_at = utcnow()
    record_event(
        session,
        run.id,
        "step_dead" if final_status == StepStatus.DEAD else "step_failed",
        step_id=step.id,
        payload={"step": step.step_name, "attempt": step.attempt, "error": error},
    )
    enqueue(
        session,
        "run.finished",
        {"run_id": run.id, "status": RunStatus.FAILED, "error": run.error},
    )
    session.commit()
    duration = time.perf_counter() - started
    metrics.inc_step(final_status)
    metrics.observe_step(step.node_type, final_status, duration)
    logger.error("step.%s step=%s error=%s", final_status.lower(), step.step_name, error)
