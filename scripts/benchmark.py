"""引擎开销压测：创建 N 个运行，起 M 个 Worker，统计吞吐和 P50/P95。

注意：默认 demo_pipeline 不含 LLM 调用，测的是调度/状态机/数据库开销，
不是模型延迟。压测数字必须连同配置一起说，否则没有意义。

用法：
    python scripts/benchmark.py --runs 50 --workers 4
"""
from __future__ import annotations

import argparse
import statistics
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.core.clock import as_utc, utcnow
from app.db import SessionLocal, init_db
from app.models import WorkflowRun
from app.services.runs import create_run
from app.services.workflows import seed_builtin_workflows
from app.worker.main import Worker

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELED"}


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    index = min(len(values) - 1, max(0, int(round((p / 100) * (len(values) - 1)))))
    return values[index]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--workflow", default="demo_pipeline")
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()

    init_db()
    with SessionLocal() as session:
        definitions = {d.name: d for d in seed_builtin_workflows(session)}
    workflow = definitions[args.workflow]

    run_ids: list[int] = []
    for _ in range(args.runs):
        with SessionLocal() as session:
            run, _ = create_run(session, workflow, {})
            run_ids.append(run.id)

    print(f"配置: workflow={args.workflow} runs={args.runs} workers={args.workers}")
    stop = threading.Event()
    workers = [Worker(worker_id=f"bench-{i}", concurrency=2) for i in range(args.workers)]
    threads = [threading.Thread(target=w.run, daemon=True) for w in workers]
    started = time.perf_counter()
    for thread in threads:
        thread.start()

    deadline = time.time() + args.timeout
    terminal: list[WorkflowRun] = []
    while time.time() < deadline:
        with SessionLocal() as session:
            terminal = list(
                session.scalars(
                    select(WorkflowRun).where(
                        WorkflowRun.id.in_(run_ids),
                        WorkflowRun.status.in_(TERMINAL),
                    )
                )
            )
        if len(terminal) == len(run_ids):
            break
        time.sleep(0.2)

    elapsed = time.perf_counter() - started
    for worker in workers:
        worker.stop()
    for thread in threads:
        thread.join(timeout=5)

    success = [r for r in terminal if r.status == "SUCCEEDED"]
    durations = [
        (as_utc(r.finished_at) - as_utc(r.created_at)).total_seconds()
        for r in success
        if r.finished_at and r.created_at
    ]
    # 每个 run 有 3 个步骤
    steps_done = len(success) * 3

    print("\n结果")
    print(f"  完成: {len(terminal)}/{len(run_ids)}，成功: {len(success)}")
    print(f"  总耗时: {elapsed:.2f}s")
    print(f"  运行吞吐: {len(success) / elapsed:.2f} runs/s")
    print(f"  步骤吞吐: {steps_done / elapsed:.2f} steps/s")
    if durations:
        print(f"  run P50: {percentile(durations, 50):.3f}s  P95: {percentile(durations, 95):.3f}s")
        print(f"  run avg: {statistics.mean(durations):.3f}s")
    if len(terminal) < len(run_ids):
        print("  [WARN] 有运行未在超时内完成")
    return 0 if len(success) == len(run_ids) else 1


if __name__ == "__main__":
    raise SystemExit(main())
