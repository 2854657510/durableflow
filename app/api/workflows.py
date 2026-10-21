from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require_api_key
from app.db import get_db
from app.models import WorkflowDefinition
from app.schemas import WorkflowCreate, WorkflowOut
from app.services.workflows import WorkflowSpecError, get_workflow, register_workflow

router = APIRouter(
    prefix="/workflows",
    tags=["workflows"],
    dependencies=[Depends(require_api_key)],
)


@router.post("", response_model=WorkflowOut)
def create_workflow(payload: WorkflowCreate, db: Session = Depends(get_db)):
    try:
        definition = register_workflow(db, payload.name, payload.spec, version=payload.version)
    except WorkflowSpecError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return definition


@router.get("", response_model=list[WorkflowOut])
def list_workflows(db: Session = Depends(get_db)):
    return list(db.scalars(select(WorkflowDefinition).order_by(WorkflowDefinition.id)))


@router.get("/{workflow_id}", response_model=WorkflowOut)
def read_workflow(workflow_id: int, db: Session = Depends(get_db)):
    definition = get_workflow(db, workflow_id)
    if definition is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    return definition
