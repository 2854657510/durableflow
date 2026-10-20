import uuid

from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app
from app.models import WorkflowDefinition
from app.services.runs import create_run, get_events


def _make_terminal_run() -> int:
    with SessionLocal() as session:
        workflow = WorkflowDefinition(
            name=f"sse-{uuid.uuid4().hex}",
            version=1,
            spec={"steps": []},
        )
        session.add(workflow)
        session.commit()
        session.refresh(workflow)
        run, _ = create_run(session, workflow, {"question": "demo"})
        return run.id


def test_stream_sends_events_and_done():
    run_id = _make_terminal_run()

    with TestClient(app) as client:
        with client.stream("GET", f"/api/runs/{run_id}/events/stream") as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            body = "".join(response.iter_text())

    assert "retry: 3000" in body
    assert "event: run_created" in body
    assert "event: done" in body


def test_stream_resumes_from_last_event_id():
    run_id = _make_terminal_run()
    with SessionLocal() as session:
        first_event = get_events(session, run_id)[0]
        first_id = first_event.id

    with TestClient(app) as client:
        with client.stream(
            "GET",
            f"/api/runs/{run_id}/events/stream",
            headers={"Last-Event-ID": str(first_id)},
        ) as response:
            body = "".join(response.iter_text())

    # 已经消费过的事件不再补发，但仍然要收到结束信号。
    assert "event: run_created" not in body
    assert "event: done" in body


def test_stream_sends_all_events_before_done_when_backlog_exceeds_batch():
    run_id = _make_terminal_run()
    with SessionLocal() as session:
        from app.core.events import record_event

        for index in range(250):
            record_event(session, run_id, "demo_event", payload={"index": index})
        session.commit()

    with TestClient(app) as client:
        with client.stream("GET", f"/api/runs/{run_id}/events/stream") as response:
            body = "".join(response.iter_text())

    assert body.count("event: demo_event") == 250
    # done 必须排在最后一条事件之后。
    assert body.rfind("event: demo_event") < body.index("event: done")


def test_stream_rejects_bad_last_event_id():
    run_id = _make_terminal_run()

    with TestClient(app) as client:
        response = client.get(
            f"/api/runs/{run_id}/events/stream",
            headers={"Last-Event-ID": "not-a-number"},
        )

    assert response.status_code == 400
