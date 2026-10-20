import asyncio
import json
import time

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from starlette.concurrency import run_in_threadpool

from app.api.deps import require_api_key
from app.config import get_settings
from app.db import SessionLocal, get_db
from app.models import Event, StepRun, WorkflowRun
from app.schemas import CostOut, EventOut, RunCreate, RunCreatedOut, RunOut, StepOut
from app.services.runs import (
    RunError,
    cancel_run,
    create_run,
    get_cost_breakdown,
    get_events,
    get_run,
    list_runs,
    retry_step,
)
from app.services.workflows import get_workflow
from app.state_machine import TERMINAL_RUN_STATUSES

router = APIRouter(
    prefix="/api/runs",
    tags=["runs"],
    dependencies=[Depends(require_api_key)],
)
start_router = APIRouter(
    prefix="/api/workflows",
    tags=["runs"],
    dependencies=[Depends(require_api_key)],
)


@start_router.post("/{workflow_id}/runs", response_model=RunCreatedOut)
def start_run(
    workflow_id: int,
    payload: RunCreate,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
):
    workflow = get_workflow(db, workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    run, created = create_run(
        db,
        workflow,
        payload.input,
        idempotency_key=idempotency_key,
        max_cost=payload.max_cost,
    )
    response.status_code = 201 if created else 200
    return RunCreatedOut(run=run, created=created)


@router.get("", response_model=list[RunOut])
def read_runs(
    limit: int = Query(default=50, ge=1, le=500),
    status: str | None = None,
    db: Session = Depends(get_db),
):
    return list_runs(db, limit=limit, status=status)


@router.get("/{run_id}", response_model=RunOut)
def read_run(run_id: int, db: Session = Depends(get_db)):
    run = get_run(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@router.get("/{run_id}/steps", response_model=list[StepOut])
def read_steps(run_id: int, db: Session = Depends(get_db)):
    if get_run(db, run_id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    return list(
        db.scalars(select(StepRun).where(StepRun.run_id == run_id).order_by(StepRun.position))
    )


@router.get("/{run_id}/events", response_model=list[EventOut])
def read_events(run_id: int, after_id: int = 0, db: Session = Depends(get_db)):
    if get_run(db, run_id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    return get_events(db, run_id, after_id=after_id)


def _run_exists(run_id: int) -> bool:
    with SessionLocal() as session:
        return session.get(WorkflowRun, run_id) is not None


def _fetch_event_batch(run_id: int, after_id: int, limit: int) -> tuple[list[dict], str | None]:
    """一次短查询：取事件 + 当时运行状态。不跨请求持有 Session。"""
    with SessionLocal() as session:
        run = session.get(WorkflowRun, run_id)
        if run is None:
            return [], None
        rows = list(
            session.scalars(
                select(Event)
                .where(Event.run_id == run_id, Event.id > after_id)
                .order_by(Event.id)
                .limit(limit)
            )
        )
        events = [
            {
                "id": row.id,
                "type": row.type,
                "step_id": row.step_id,
                "payload": row.payload,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]
        return events, run.status


async def _event_stream(run_id: int, after_id: int, request: Request):
    settings = get_settings()
    cursor = after_id
    last_heartbeat = time.monotonic()
    # 告诉浏览器断线后 3 秒再重连；重连时浏览器会自动带上 Last-Event-ID。
    yield "retry: 3000\n\n"

    while True:
        if await request.is_disconnected():
            return

        events, status = await run_in_threadpool(
            _fetch_event_batch, run_id, cursor, settings.sse_batch_size
        )
        for event in events:
            cursor = event["id"]
            data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
            yield f"id: {cursor}\nevent: {event['type']}\ndata: {data}\n\n"

        # 有积压事件时先把事件补完，再判断是否终态；否则超过一批的尾部事件会丢。
        if len(events) >= settings.sse_batch_size:
            continue

        if status is None:
            yield 'event: done\ndata: {"status":"NOT_FOUND"}\n\n'
            return
        if status in TERMINAL_RUN_STATUSES:
            payload = json.dumps({"run_id": run_id, "status": status}, ensure_ascii=False)
            yield f"event: done\ndata: {payload}\n\n"
            return

        now = time.monotonic()
        if now - last_heartbeat >= settings.sse_heartbeat_seconds:
            # SSE 注释行，用来穿过 Nginx / 负载均衡的空闲超时。
            yield ": ping\n\n"
            last_heartbeat = now
        await asyncio.sleep(settings.sse_poll_seconds)


@router.get("/{run_id}/events/stream")
async def stream_events(
    run_id: int,
    request: Request,
    after_id: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    if not await run_in_threadpool(_run_exists, run_id):
        raise HTTPException(status_code=404, detail="run not found")

    cursor = after_id
    if last_event_id:
        try:
            cursor = max(cursor, int(last_event_id))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Last-Event-ID 必须是整数") from exc

    return StreamingResponse(
        _event_stream(run_id, cursor, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{run_id}/cancel", response_model=RunOut)
def cancel(run_id: int, db: Session = Depends(get_db)):
    try:
        return cancel_run(db, run_id)
    except RunError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/steps/{step_id}/retry", response_model=StepOut)
def retry(step_id: int, db: Session = Depends(get_db)):
    try:
        return retry_step(db, step_id)
    except RunError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{run_id}/cost", response_model=CostOut)
def cost(run_id: int, db: Session = Depends(get_db)):
    if get_run(db, run_id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    return get_cost_breakdown(db, run_id)
