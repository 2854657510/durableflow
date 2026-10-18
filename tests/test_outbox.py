from sqlalchemy import select

from app.core.outbox import enqueue, relay_once
from app.models import Outbox


def test_outbox_relay_marks_sent(session):
    enqueue(session, "demo.event", {"x": 1})
    session.commit()
    received = []
    result = relay_once(session, publisher=lambda t, p: received.append((t, p)))
    assert result["sent"] == 1
    assert received == [("demo.event", {"x": 1})]
    row = session.scalar(select(Outbox))
    assert row.status == "SENT"


def test_outbox_retries_on_failure(session):
    enqueue(session, "demo.event", {"x": 1})
    session.commit()

    def fail(t, p):
        raise RuntimeError("downstream down")

    result = relay_once(session, publisher=fail)
    assert result["failed"] == 0
    row = session.scalar(select(Outbox))
    assert row.status == "PENDING"
    assert row.attempts == 1
