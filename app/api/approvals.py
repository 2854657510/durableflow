from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require_api_key
from app.db import get_db
from app.models import Approval
from app.schemas import ApprovalDecision, ApprovalOut
from app.services.runs import RunError, decide_approval

router = APIRouter(
    prefix="/approvals",
    tags=["approvals"],
    dependencies=[Depends(require_api_key)],
)


@router.get("", response_model=list[ApprovalOut])
def list_approvals(
    status: str | None = Query(default="PENDING"),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    stmt = select(Approval).order_by(Approval.id.desc()).limit(limit)
    if status:
        stmt = stmt.where(Approval.status == status)
    return list(db.scalars(stmt))


@router.post("/{approval_id}/approve", response_model=ApprovalOut)
def approve(
    approval_id: int,
    payload: ApprovalDecision,
    request: Request,
    db: Session = Depends(get_db),
):
    try:
        return decide_approval(
            db,
            approval_id,
            approved=True,
            decided_by=payload.decided_by,
            comment=payload.comment,
            request_id=getattr(request.state, "request_id", None),
            actor_ip=request.client.host if request.client else None,
        )
    except RunError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{approval_id}/reject", response_model=ApprovalOut)
def reject(
    approval_id: int,
    payload: ApprovalDecision,
    request: Request,
    db: Session = Depends(get_db),
):
    try:
        return decide_approval(
            db,
            approval_id,
            approved=False,
            decided_by=payload.decided_by,
            comment=payload.comment,
            request_id=getattr(request.state, "request_id", None),
            actor_ip=request.client.host if request.client else None,
        )
    except RunError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
