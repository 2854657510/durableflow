from __future__ import annotations

import logging
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait

from sqlalchemy import func, select

from app.config import get_settings
from app.db import SessionLocal, init_db
from app.models import StepRun
from app.observability import metrics
from app.observability.logging import configure_logging
from app.observability.tracing import setup_tracing
from app.state_machine import StepStatus
from app.worker.claim import claim_steps
from app.worker.executor import execute_step
from app.worker.lease import LeaseHeartbeat, ReaperLoop, mark_offline, register_worker

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, worker_id: str | None = None, concurrency: int | None = None):
        self.settings = get_settings()
        self.worker_id = worker_id or self.settings.worker_id
        self.concurrency = max(1, concurrency or self.settings.worker_concurrency)
        self._stop = threading.Event()
        self._heartbeat: LeaseHeartbeat | None = None
        self._reaper: ReaperLoop | None = None
        self._pool: ThreadPoolExecutor | None = None

    def stop(self) -> None:
        self._stop.set()

    def _queue_depth(self) -> int:
        with SessionLocal() as session:
            return int(
                session.scalar(
                    select(func.count()).select_from(StepRun).where(
                        StepRun.status.in_([StepStatus.PENDING, StepStatus.RETRYING])
                    )
                )
                or 0
            )

    def run(self) -> None:
        setup_tracing()
        init_db()
        with SessionLocal() as session:
            register_worker(session, self.worker_id, self.concurrency)

        self._heartbeat = LeaseHeartbeat(
            self.worker_id,
            self.settings.heartbeat_seconds,
            self.settings.lease_seconds,
        )
        self._heartbeat.start()
        self._reaper = ReaperLoop(self.settings.reaper_interval_seconds)
        self._reaper.start()
        self._pool = ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="step")

        logger.info(
            "worker.started id=%s concurrency=%s lease=%ss",
            self.worker_id,
            self.concurrency,
            self.settings.lease_seconds,
        )

        try:
            while not self._stop.is_set():
                with SessionLocal() as session:
                    steps = claim_steps(
                        session,
                        worker_id=self.worker_id,
                        batch_size=max(self.settings.batch_size, self.concurrency),
                        lease_seconds=self.settings.lease_seconds,
                    )
                metrics.set_queue_depth(self._queue_depth())
                if not steps:
                    self._stop.wait(self.settings.poll_interval_seconds)
                    continue

                futures = []
                for step in steps:
                    assert self._heartbeat is not None
                    self._heartbeat.add(step.id)
                    future = self._pool.submit(execute_step, step.id, self.worker_id)
                    future.add_done_callback(lambda f, sid=step.id: self._on_done(f, sid))
                    futures.append(future)
                wait(futures)
                metrics.set_queue_depth(self._queue_depth())
        except KeyboardInterrupt:
            logger.info("worker interrupted")
        finally:
            self._shutdown()

    def _on_done(self, future, step_id: int) -> None:
        if self._heartbeat is not None:
            self._heartbeat.remove(step_id)
        try:
            future.result()
        except Exception:  # noqa: BLE001
            logger.exception("step executor crashed step_id=%s", step_id)

    def _shutdown(self) -> None:
        self._stop.set()
        if self._pool is not None:
            self._pool.shutdown(wait=True)
        if self._heartbeat is not None:
            self._heartbeat.stop()
        if self._reaper is not None:
            self._reaper.stop()
        with SessionLocal() as session:
            mark_offline(session, self.worker_id)
        logger.info("worker.stopped id=%s", self.worker_id)


def run_worker_once(worker_id: str = "worker-once", limit: int = 1) -> list[int]:
    """测试/演示用：领取并执行一批步骤，返回执行的 step id。"""
    init_db()
    with SessionLocal() as session:
        register_worker(session, worker_id, 1)
        steps = claim_steps(
            session,
            worker_id=worker_id,
            batch_size=limit,
            lease_seconds=get_settings().lease_seconds,
        )
    ids = [s.id for s in steps]
    for step_id in ids:
        execute_step(step_id, worker_id)
    return ids


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    worker = Worker()

    def _handle_signal(signum, frame):  # noqa: ARG001
        logger.info("worker.signal signum=%s", signum)
        worker.stop()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    worker.run()


if __name__ == "__main__":
    main()
