from __future__ import annotations

import logging
import threading
import time
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.events import record_event
from app.core.outbox import relay_once
from app.db import SessionLocal
from app.models import Outbox, StepRun, Worker, WorkflowRun
from app.observability import metrics
from app.services.runs import refresh_run_status
from app.state_machine import RunStatus, StepStatus

logger = logging.getLogger(__name__)


def register_worker(session: Session, worker_id: str, concurrency: int) -> Worker:
    worker = session.get(Worker, worker_id)
    if worker is None:
        worker = Worker(id=worker_id, status="ONLINE", concurrency=concurrency, last_heartbeat_at=utcnow())
        session.add(worker)
    else:
        worker.status = "ONLINE"
        worker.concurrency = concurrency
        worker.last_heartbeat_at = utcnow()
    session.commit()
    return worker


def worker_heartbeat(session: Session, worker_id: str) -> None:
    worker = session.get(Worker, worker_id)
    if worker is None:
        worker = Worker(id=worker_id, status="ONLINE", concurrency=1, last_heartbeat_at=utcnow())
        session.add(worker)
    else:
        worker.status = "ONLINE"
        worker.last_heartbeat_at = utcnow()
    session.commit()


def mark_offline(session: Session, worker_id: str) -> None:
    worker = session.get(Worker, worker_id)
    if worker is not None:
        worker.status = "OFFLINE"
        worker.last_heartbeat_at = utcnow()
        session.commit()


def extend_lease(session: Session, step_id: int, worker_id: str, lease_seconds: int) -> bool:
    step = session.get(StepRun, step_id)
    if step is None or step.status != StepStatus.RUNNING or step.lease_owner != worker_id:
        return False
    step.heartbeat_at = utcnow()
    step.lease_expires_at = utcnow() + timedelta(seconds=lease_seconds)
    session.commit()
    return True


def reap_expired_leases(session: Session, *, now=None) -> list[int]:
    """回收过期租约。

    这是“Worker 崩了任务还在”的核心：租约过期后步骤重新入队，
    副作用由 StepEffect/Outbox 保证不重复。
    """
    now = now or utcnow()
    stmt = select(StepRun).where(
        StepRun.status == StepStatus.RUNNING,
        StepRun.lease_expires_at.is_not(None),
        StepRun.lease_expires_at < now,
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)

    steps = list(session.scalars(stmt))
    affected_runs: set[int] = set()
    reaped: list[int] = []
    for step in steps:
        run = session.get(WorkflowRun, step.run_id)
        affected_runs.add(step.run_id)
        step.lease_owner = None
        step.lease_expires_at = None
        if run is not None and run.status == RunStatus.CANCELED:
            step.status = StepStatus.CANCELED
            step.finished_at = now
            event_type = "step_canceled"
        elif step.attempt >= step.max_attempts:
            step.status = StepStatus.DEAD
            step.error = "lease expired; max attempts reached"
            step.finished_at = now
            event_type = "step_dead"
        else:
            step.status = StepStatus.PENDING
            step.available_at = now
            step.error = "lease expired"
            event_type = "step_reaped"
        metrics.inc_lease_expired()
        record_event(session, step.run_id, event_type, step_id=step.id, payload={"step": step.step_name})
        reaped.append(step.id)

    for run_id in affected_runs:
        run = session.get(WorkflowRun, run_id)
        if run is not None:
            refresh_run_status(session, run)
    session.commit()
    return reaped


class LeaseHeartbeat(threading.Thread):
    def __init__(self, worker_id: str, interval: int, lease_seconds: int):
        super().__init__(daemon=True, name=f"heartbeat-{worker_id}")
        self.worker_id = worker_id
        self.interval = max(1, interval)
        self.lease_seconds = lease_seconds
        self._active: dict[int, str] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def add(self, step_id: int) -> None:
        with self._lock:
            self._active[step_id] = self.worker_id

    def remove(self, step_id: int) -> None:
        with self._lock:
            self._active.pop(step_id, None)

    def run(self) -> None:
        while not self._stop.wait(self.interval):
            with self._lock:
                active = list(self._active.keys())
            if not active:
                continue
            try:
                with SessionLocal() as session:
                    worker_heartbeat(session, self.worker_id)
                    for step_id in active:
                        extend_lease(session, step_id, self.worker_id, self.lease_seconds)
            except Exception:  # noqa: BLE001
                logger.exception("lease heartbeat failed")

    def stop(self) -> None:
        self._stop.set()


class ReaperLoop(threading.Thread):
    def __init__(self, interval: int):
        super().__init__(daemon=True, name="reaper")
        self.interval = max(1, interval)
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                with SessionLocal() as session:
                    reaped = reap_expired_leases(session)
                    if reaped:
                        logger.warning("reaped %s expired steps", len(reaped))
                    relay_once(session)
                    oldest = session.scalar(
                        select(Outbox.available_at)
                        .where(Outbox.status == "PENDING")
                        .order_by(Outbox.available_at)
                        .limit(1)
                    )
                    if oldest is not None:
                        from app.core.clock import as_utc

                        metrics.set_outbox_lag((utcnow() - as_utc(oldest)).total_seconds())
            except Exception:  # noqa: BLE001
                logger.exception("reaper loop failed")

    def stop(self) -> None:
        self._stop.set()
