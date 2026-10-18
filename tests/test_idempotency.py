from sqlalchemy import func, select

from app.agent.loop import AgentLoop
from app.agent.memory import MemoryStore
from app.agent.tools import default_registry
from app.core.context import ContextManager
from app.llm.client import MockLLMClient
from app.models import StepEffect
from app.services.runs import create_run
from app.worker.claim import claim_steps


def test_create_run_is_idempotent(session, workflows):
    r1, created1 = create_run(session, workflows["demo_pipeline"], {}, idempotency_key="k-1")
    r2, created2 = create_run(session, workflows["demo_pipeline"], {}, idempotency_key="k-1")
    assert created1 is True
    assert created2 is False
    assert r1.id == r2.id


def test_step_effect_is_recorded_once(session, workflows):
    run, _ = create_run(session, workflows["demo_pipeline"], {})
    step = claim_steps(session, worker_id="w1", batch_size=1, lease_seconds=30)[0]
    loop = AgentLoop(MockLLMClient(), default_registry(), MemoryStore(), ContextManager())

    first = loop._record_effect(session, run.id, step.id, "email:1", {"sent": True})
    second = loop._record_effect(session, run.id, step.id, "email:1", {"sent": True})
    session.commit()

    count = session.scalar(select(func.count()).select_from(StepEffect))
    assert count == 1
    assert first == second == {"sent": True}
