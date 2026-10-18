from sqlalchemy import select

from app.models import Approval, WorkflowRun
from app.services.runs import create_run, decide_approval
from app.worker.main import run_worker_once


def _drive(run_id: int, *, approve: bool = False, rounds: int = 12) -> WorkflowRun:
    from app.db import SessionLocal

    for i in range(rounds):
        run_worker_once(worker_id=f"test-{i}", limit=5)
        with SessionLocal() as session:
            run = session.get(WorkflowRun, run_id)
            if approve and run.status == "WAITING_APPROVAL":
                approval = session.scalar(select(Approval).where(Approval.status == "PENDING"))
                if approval is not None:
                    decide_approval(session, approval.id, approved=True, decided_by="test")
            if run.status in {"SUCCEEDED", "FAILED", "CANCELED"}:
                return run
    return run


def test_demo_pipeline_succeeds(session, workflows):
    run, _ = create_run(session, workflows["demo_pipeline"], {})
    result = _drive(run.id)
    assert result.status == "SUCCEEDED"


def test_agent_review_with_approval_succeeds(session, workflows):
    run, _ = create_run(session, workflows["agent_review"], {})
    result = _drive(run.id, approve=True, rounds=20)
    assert result.status == "SUCCEEDED"
    assert result.output is not None
    assert "report" in result.output


def test_budget_exceeded_fails_run(session, workflows):
    from app.db import SessionLocal
    from app.services.runs import get_run

    run, _ = create_run(session, workflows["agent_review"], {}, max_cost=0.0)
    run_worker_once(worker_id="budget-worker", limit=5)
    with SessionLocal() as fresh:
        current = get_run(fresh, run.id)
        assert current.status == "FAILED"
        assert "预算" in (current.error or "")
