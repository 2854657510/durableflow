from sqlalchemy import select

from app.models import StepRun, WorkflowRun
from app.services.runs import create_run
from app.worker.claim import claim_steps
from app.worker.executor import execute_step


def test_claim_is_exclusive_and_releases_next_step(session, workflows):
    workflow = workflows["demo_pipeline"]
    run, _ = create_run(session, workflow, {})
    run_id = run.id

    claimed = claim_steps(session, worker_id="w1", batch_size=5, lease_seconds=30)
    assert len(claimed) == 1
    assert claimed[0].step_name == "prepare"

    again = claim_steps(session, worker_id="w2", batch_size=5, lease_seconds=30)
    assert again == []

    execute_step(claimed[0].id, "w1")
    session.expire_all()

    steps = list(session.scalars(select(StepRun).where(StepRun.run_id == run_id).order_by(StepRun.position)))
    assert steps[0].status == "SUCCEEDED"
    assert steps[1].status == "PENDING"
    assert steps[2].status == "BLOCKED"

    for _ in range(3):
        claimed = claim_steps(session, worker_id="w1", batch_size=5, lease_seconds=30)
        if not claimed:
            break
        for step in claimed:
            execute_step(step.id, "w1")

    session.expire_all()
    run = session.get(WorkflowRun, run_id)
    assert run.status == "SUCCEEDED"
