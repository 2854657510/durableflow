from __future__ import annotations

import threading
from datetime import timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.models import StepRun

# SQLite 没有 SKIP LOCKED，单进程内用一把锁保证不重复领取。
_SQLITE_CLAIM_LOCK = threading.Lock()


def claim_steps(
    session: Session,
    *,
    worker_id: str,
    batch_size: int = 5,
    lease_seconds: int = 30,
) -> list[StepRun]:
    dialect = session.bind.dialect.name if session.bind is not None else ""

    if dialect == "postgresql":
        sql = text(
            """
            WITH picked AS (
                SELECT id
                FROM step_runs
                WHERE status IN ('PENDING', 'RETRYING')
                  AND available_at <= clock_timestamp()
                ORDER BY available_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT :batch_size
            )
            UPDATE step_runs s
            SET status = 'RUNNING',
                lease_owner = :worker_id,
                lease_expires_at = clock_timestamp() + make_interval(secs => :lease_seconds),
                heartbeat_at = clock_timestamp(),
                started_at = COALESCE(s.started_at, clock_timestamp()),
                attempt = s.attempt + 1
            FROM picked
            WHERE s.id = picked.id
            RETURNING s.id
            """
        )
        ids = [row[0] for row in session.execute(
            sql,
            {"batch_size": batch_size, "worker_id": worker_id, "lease_seconds": lease_seconds},
        )]
    elif dialect == "sqlite":
        with _SQLITE_CLAIM_LOCK:
            now = utcnow()
            expires = now + timedelta(seconds=lease_seconds)
            candidate_ids = list(
                session.scalars(
                    select(StepRun.id)
                    .where(
                        StepRun.status.in_(["PENDING", "RETRYING"]),
                        StepRun.available_at <= now,
                    )
                    .order_by(StepRun.available_at, StepRun.id)
                    .limit(batch_size)
                )
            )
            ids = []
            for step_id in candidate_ids:
                result = session.execute(
                    text(
                        """
                        UPDATE step_runs
                        SET status = 'RUNNING',
                            lease_owner = :worker_id,
                            lease_expires_at = :expires,
                            heartbeat_at = :now,
                            started_at = COALESCE(started_at, :now),
                            attempt = attempt + 1
                        WHERE id = :id AND status IN ('PENDING', 'RETRYING')
                        """
                    ),
                    {"worker_id": worker_id, "expires": expires, "now": now, "id": step_id},
                )
                if result.rowcount:
                    ids.append(step_id)
            session.commit()
    else:
        raise RuntimeError(f"不支持的数据库方言: {dialect}")

    if not ids:
        session.commit()
        return []

    steps = list(session.scalars(select(StepRun).where(StepRun.id.in_(ids)).order_by(StepRun.id)))
    session.commit()
    return steps
