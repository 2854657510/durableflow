"""真实崩溃恢复演示：长步骤执行中 kill Worker，另一个 Worker 在租约过期后接手。

用法：
    python scripts/crash_recovery.py

脚本会起两个子进程：
1. crash-1 领取 slow 步骤并开始 sleep；
2. kill crash-1；
3. crash-2 在租约过期后 reaper 回收，重新执行并完成。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.models import StepRun, WorkflowRun
from app.services.runs import create_run, get_run
from app.services.workflows import seed_builtin_workflows


def _step(run_id: int) -> StepRun | None:
    with SessionLocal() as session:
        return session.scalar(
            select(StepRun).where(StepRun.run_id == run_id, StepRun.step_name == "slow")
        )


def _spawn(worker_id: str, log_name: str) -> subprocess.Popen:
    env = os.environ.copy()
    env.update(
        WORKER_ID=worker_id,
        WORKER_CONCURRENCY="1",
        LEASE_SECONDS="3",
        HEARTBEAT_SECONDS="1",
        REAPER_INTERVAL_SECONDS="1",
        POLL_INTERVAL_SECONDS="0.2",
        LOG_LEVEL="INFO",
    )
    log = open(ROOT / log_name, "w", encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, "-m", "app.worker.main"],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )


def main() -> int:
    init_db()
    with SessionLocal() as session:
        definitions = {d.name: d for d in seed_builtin_workflows(session)}
        run, _ = create_run(session, definitions["crash_recovery"], {})
    run_id = run.id
    print(f"创建运行 #{run_id}，启动 crash-1")

    w1 = _spawn("crash-1", "_crash_worker1.log")
    started = None
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            step = _step(run_id)
            if step is not None and step.started_at is not None and step.status == "RUNNING":
                started = step.started_at
                print(f"crash-1 已领取 slow 步骤: status={step.status} attempt={step.attempt} lease={step.lease_expires_at}")
                break
            time.sleep(0.2)
        if started is None:
            print("没有等到 crash-1 领取步骤，检查 _crash_worker1.log")
            return 1

        time.sleep(0.5)
        print("kill crash-1 ...")
        w1.kill()
        w1.wait(timeout=10)
        kill_at = time.time()

        step = _step(run_id)
        print(f"kill 后步骤状态: {step.status if step else 'missing'} lease={step.lease_expires_at if step else '-'}")

        print("启动 crash-2，等待租约过期和自动恢复 ...")
        w2 = _spawn("crash-2", "_crash_worker2.log")
        try:
            deadline = time.time() + 40
            while time.time() < deadline:
                with SessionLocal() as session:
                    current = get_run(session, run_id)
                    assert current is not None
                    if current.status in {"SUCCEEDED", "FAILED", "CANCELED"}:
                        elapsed = time.time() - kill_at
                        print(f"最终状态: {current.status}，kill 到完成耗时 {elapsed:.2f}s")
                        print(f"输出: {current.output}")
                        print("步骤状态:")
                        for st in session.scalars(
                            select(StepRun).where(StepRun.run_id == run_id).order_by(StepRun.position)
                        ):
                            print(f"  - {st.step_name:<8} {st.status:<10} attempt={st.attempt}/{st.max_attempts}")
                        return 0 if current.status == "SUCCEEDED" else 2
                time.sleep(0.3)
            print("超时未恢复，检查 _crash_worker2.log")
            return 3
        finally:
            w2.terminate()
            try:
                w2.wait(timeout=10)
            except subprocess.TimeoutExpired:
                w2.kill()
    finally:
        if w1.poll() is None:
            w1.kill()


if __name__ == "__main__":
    raise SystemExit(main())
