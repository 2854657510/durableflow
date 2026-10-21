from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require_api_key
from app.db import get_db
from app.models import Worker
from app.schemas import WorkerOut

router = APIRouter(
    prefix="/workers",
    tags=["workers"],
    dependencies=[Depends(require_api_key)],
)


@router.get("", response_model=list[WorkerOut])
def list_workers(db: Session = Depends(get_db)):
    return list(db.scalars(select(Worker).order_by(Worker.id)))
