from app.agent.loop import AgentLoop
from app.agent.memory import MemoryStore
from app.agent.tools import default_registry
from app.core.context import ContextManager
from app.llm.client import MockLLMClient
from app.services.runs import create_run
from app.worker.claim import claim_steps


def _new_loop():
    return AgentLoop(
        MockLLMClient(),
        default_registry(),
        MemoryStore(),
        ContextManager(max_tokens=2000, keep_last_messages=4),
        max_steps=5,
        namespace="test",
    )


def test_agent_calls_tool_then_answers(session, workflows):
    run, _ = create_run(session, workflows["dangerous_agent"], {})
    step = claim_steps(session, worker_id="w1", batch_size=1, lease_seconds=30)[0]
    result = _new_loop().run(
        session,
        run_id=run.id,
        step_id=step.id,
        goal="整理项目资料并给出结论",
    )
    session.commit()
    assert result.status == "completed"
    assert result.steps[0]["tool"] == "search_docs"
    assert result.steps[-1]["status"] == "final"


def test_agent_pauses_for_dangerous_tool(session, workflows):
    run, _ = create_run(session, workflows["dangerous_agent"], {})
    step = claim_steps(session, worker_id="w1", batch_size=1, lease_seconds=30)[0]
    result = _new_loop().run(
        session,
        run_id=run.id,
        step_id=step.id,
        goal="发送邮件给 ops@example.com",
    )
    session.commit()
    assert result.status == "pending_approval"
    assert result.pending_tool == "send_email"
