from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import require_api_key
from app.db import get_db
from app.models import Event, StepRun
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
