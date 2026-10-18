"""A/B 对比两个工作流版本：成功率、P50/P95、token、成本。

用途：回答“版本更新以后怎么证明效果提升了”。
前提：两个版本已经注册（POST /api/workflows 或直接写库）。

用法：
    python scripts/compare_versions.py --name demo_pipeline --a 1 --b 2 --runs 30 --workers 2
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

from app.core.clock import as_utc
from app.db import SessionLocal, init_db
from app.models import WorkflowDefinition, WorkflowRun
from app.services.runs import create_run
from app.services.workflows import get_workflow_by_name
from app.worker.main import Worker

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELED"}


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, max(0, int(round((p / 100) * (len(values) - 1)))))]


def run_batch(workflow: WorkflowDefinition, runs: int, workers: int, timeout: int) -> dict:
    run_ids: list[int] = []
    for _ in range(runs):
        with SessionLocal() as session:
            row, _ = create_run(session, workflow, {})
            run_ids.append(row.id)

    workers_list = [Worker(worker_id=f"cmp-{workflow.version}-{i}", concurrency=2) for i in range(workers)]
    threads = [threading.Thread(target=w.run, daemon=True) for w in workers_list]
    started = time.perf_counter()
    for thread in threads:
        thread.start()

    deadline = time.time() + timeout
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
    for worker in workers_list:
        worker.stop()
    for thread in threads:
        thread.join(timeout=5)

    success = [r for r in terminal if r.status == "SUCCEEDED"]
    durations = [
        (as_utc(r.finished_at) - as_utc(r.created_at)).total_seconds()
        for r in success
        if r.finished_at and r.created_at
    ]
    return {
        "runs": len(terminal),
        "success": len(success),
        "success_rate": len(success) / max(1, len(terminal)),
        "elapsed": elapsed,
        "p50": percentile(durations, 50),
        "p95": percentile(durations, 95),
        "avg_duration": statistics.mean(durations) if durations else 0.0,
        "avg_tokens": statistics.mean([r.total_tokens for r in success]) if success else 0.0,
        "avg_cost": statistics.mean([r.total_cost for r in success]) if success else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="demo_pipeline")
    parser.add_argument("--a", type=int, default=1)
    parser.add_argument("--b", type=int, default=2)
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()

    init_db()
    with SessionLocal() as session:
        a = get_workflow_by_name(session, args.name, args.a)
        b = get_workflow_by_name(session, args.name, args.b)
    if a is None or b is None:
        print(f"找不到版本: {args.name} v{args.a} / v{args.b}")
        print("先通过 POST /api/workflows 注册第二个版本，或修改 scripts/seed.py 增加版本。")
        return 1

    print(f"对比 {args.name} v{args.a} vs v{args.b}，每版 {args.runs} 次，{args.workers} Worker")
    result_a = run_batch(a, args.runs, args.workers, args.timeout)
    result_b = run_batch(b, args.runs, args.workers, args.timeout)

    print("\n指标                    A (v%d)        B (v%d)" % (args.a, args.b))
    print("----------------------  ------------  ------------")
    for key, label in [
        ("runs", "完成数"),
        ("success", "成功数"),
        ("success_rate", "成功率"),
        ("p50", "P50 (s)"),
        ("p95", "P95 (s)"),
        ("avg_duration", "平均耗时 (s)"),
        ("avg_tokens", "平均 token"),
        ("avg_cost", "平均成本 ($)"),
        ("elapsed", "批次总耗时 (s)"),
    ]:
        va, vb = result_a[key], result_b[key]
        if isinstance(va, float):
            print(f"{label:<22}  {va:<12.4f}  {vb:<12.4f}")
        else:
            print(f"{label:<22}  {va:<12}  {vb:<12}")

    if result_a["success_rate"] and result_b["success_rate"]:
        print(
            f"\n结论：成功率 {result_a['success_rate']:.1%} -> {result_b['success_rate']:.1%}；"
            f"P95 {result_a['p95']:.3f}s -> {result_b['p95']:.3f}s；"
            f"平均成本 ${result_a['avg_cost']:.6f} -> ${result_b['avg_cost']:.6f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
