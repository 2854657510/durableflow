from sqlalchemy.orm import Session

from app.models import Event


def record_event(
    session: Session,
    run_id: int,
    event_type: str,
    *,
    step_id: int | None = None,
    payload: dict | None = None,
) -> Event:
    event = Event(
        run_id=run_id,
        step_id=step_id,
        type=event_type,
        payload=payload or {},
    )
    session.add(event)
    return event
