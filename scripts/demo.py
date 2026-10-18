"""本地端到端演示：起一个 Worker，创建运行，打印时间线，自动审批。

用法：
    python scripts/demo.py --workflow agent_review
    python scripts/demo.py --workflow dangerous_agent --goal "整理结论并发送邮件给 ops@example.com"
    python scripts/demo.py --workflow fail_retry
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal, init_db
from app.models import Event, StepRun, WorkflowRun
from app.services.runs import create_run, decide_approval, get_cost_breakdown, get_run
from app.services.workflows import seed_builtin_workflows
from app.worker.main import Worker


def _print_run(run: WorkflowRun) -> None:
    with SessionLocal() as session:
        steps = list(
            session.scalars(
                select(StepRun).where(StepRun.run_id == run.id).order_by(StepRun.position)
            )
        )
    print(f"\n运行 #{run.id}  状态: {run.status}  tokens: {run.total_tokens}  cost: ${run.total_cost:.4f}")
    for step in steps:
        print(f"  - {step.step_name:<18} {step.status:<18} attempt={step.attempt}/{step.max_attempts}"
              + (f"  error={step.error}" if step.error else ""))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", default="agent_review")
    parser.add_argument("--goal", default="")
    parser.add_argument("--no-auto-approve", action="store_true")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()

    settings = get_settings()
    init_db()
    with SessionLocal() as session:
        definitions = {d.name: d for d in seed_builtin_workflows(session)}
    if args.workflow not in definitions:
        print(f"未知工作流: {args.workflow}，可选: {', '.join(definitions)}")
        return 1

    input_data = {"goal": args.goal} if args.goal else {}
    with SessionLocal() as session:
        run, created = create_run(session, definitions[args.workflow], input_data)
    print(f"创建运行 #{run.id} workflow={args.workflow} created={created}")

    stop = threading.Event()
    worker = Worker(worker_id=f"demo-{int(time.time())}", concurrency=2)

    def _run_worker():
        worker.run()

    thread = threading.Thread(target=_run_worker, daemon=True)
    thread.start()

    deadline = time.time() + args.timeout
    last_snapshot = None
    try:
        while time.time() < deadline:
            with SessionLocal() as session:
                run = get_run(session, run.id)
                if run is None:
                    break
                snapshot = (run.status, run.total_tokens, run.total_cost)
                if snapshot != last_snapshot:
                    _print_run(run)
                    last_snapshot = snapshot
                if run.status in {"SUCCEEDED", "FAILED", "CANCELED"}:
                    break
                if run.status == "WAITING_APPROVAL" and not args.no_auto_approve:
                    from app.models import Approval

                    approval = session.scalar(select(Approval).where(Approval.status == "PENDING"))
                    if approval is not None:
                        print(f"  -> 自动审批 approval #{approval.id} tool={approval.tool_name}")
                        decide_approval(session, approval.id, approved=True, decided_by="demo")
            time.sleep(0.5)
    finally:
        worker.stop()
        thread.join(timeout=5)

    with SessionLocal() as session:
        run = get_run(session, run.id)
        print("\n最终状态:", run.status)
        print("输出:", run.output)
        events = list(session.scalars(select(Event).where(Event.run_id == run.id).order_by(Event.id)))
        print(f"\n事件数: {len(events)}，最近 8 条：")
        for event in events[-8:]:
            print(f"  {event.type:<24} {event.payload}")
        print("\n成本明细:", get_cost_breakdown(session, run.id))
    return 0 if run.status == "SUCCEEDED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
