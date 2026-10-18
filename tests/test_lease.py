from datetime import timedelta

from sqlalchemy import select

from app.core.clock import utcnow
from app.models import StepRun, WorkflowRun
from app.services.runs import create_run
from app.worker.claim import claim_steps
from app.worker.lease import reap_expired_leases


def test_expired_lease_is_requeued(session, workflows):
    run, _ = create_run(session, workflows["demo_pipeline"], {})
    claimed = claim_steps(session, worker_id="w1", batch_size=1, lease_seconds=1)
    step = claimed[0]
    step.lease_expires_at = utcnow() - timedelta(seconds=1)
    session.commit()

    reaped = reap_expired_leases(session)
    assert step.id in reaped

    step = session.get(StepRun, step.id)
    assert step.status == "PENDING"
    assert step.lease_owner is None
    assert step.available_at is not None


def test_expired_lease_at_max_attempts_goes_dead(session, workflows):
    run, _ = create_run(session, workflows["demo_pipeline"], {})
    claimed = claim_steps(session, worker_id="w1", batch_size=1, lease_seconds=1)
    step = claimed[0]
    step.attempt = step.max_attempts
    step.lease_expires_at = utcnow() - timedelta(seconds=1)
    session.commit()

    reap_expired_leases(session)

    step = session.get(StepRun, step.id)
    run = session.get(WorkflowRun, run.id)
    assert step.status == "DEAD"
    assert run.status == "FAILED"
