from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.models import Outbox

logger = logging.getLogger(__name__)

Publisher = Callable[[str, dict], None]


def enqueue(session: Session, event_type: str, payload: dict, *, event_id: str | None = None) -> Outbox:
    """把事件写进 Outbox。调用方应保证它和业务变更在同一个事务里。"""
    row = Outbox(
        event_id=event_id or str(uuid.uuid4()),
        event_type=event_type,
        payload=payload,
        status="PENDING",
        available_at=utcnow(),
    )
    session.add(row)
    return row


def _default_publisher(event_type: str, payload: dict) -> None:
    logger.info("outbox.publish type=%s payload=%s", event_type, payload)


def relay_once(
    session: Session,
    *,
    publisher: Publisher | None = None,
    limit: int = 100,
    max_attempts: int = 5,
) -> dict:
    """投递一批 Outbox 事件。

    生产环境可以换成 Kafka/RabbitMQ；消费者必须按 event_id 去重。
    """
    publisher = publisher or _default_publisher
    stmt = (
        select(Outbox)
        .where(Outbox.status == "PENDING", Outbox.available_at <= utcnow())
        .order_by(Outbox.available_at, Outbox.id)
        .limit(limit)
    )
    # PostgreSQL 下多个 relay 进程可以无锁并发；SQLite 单测忽略。
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)

    rows = list(session.scalars(stmt))
    sent = failed = 0
    for row in rows:
        row.attempts += 1
        try:
            publisher(row.event_type, row.payload)
        except Exception as exc:  # noqa: BLE001 - relay 不能因为单条事件挂掉
            row.available_at = utcnow() + timedelta(seconds=min(60, 2 ** row.attempts))
            if row.attempts >= max_attempts:
                row.status = "FAILED"
                failed += 1
                logger.error("outbox.failed id=%s attempts=%s err=%s", row.event_id, row.attempts, exc)
            else:
                logger.warning("outbox.retry id=%s attempts=%s err=%s", row.event_id, row.attempts, exc)
        else:
            row.status = "SENT"
            row.sent_at = utcnow()
            sent += 1

    session.commit()
    return {"picked": len(rows), "sent": sent, "failed": failed}
