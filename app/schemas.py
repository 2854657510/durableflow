from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class WorkflowCreate(BaseModel):
    name: str
    version: int = 1
    spec: dict


class WorkflowOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    version: int
    spec: dict
    created_at: datetime


class RunCreate(BaseModel):
    input: dict = Field(default_factory=dict)
    max_cost: float | None = None


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    workflow_id: int
    status: str
    input: dict
    output: dict | None
    error: str | None
    total_tokens: int
    total_cost: float
    max_cost: float
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class RunCreatedOut(BaseModel):
    run: RunOut
    created: bool


class StepOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    run_id: int
    step_name: str
    node_type: str
    status: str
    attempt: int
    max_attempts: int
    depends_on: list
    output: dict | None
    error: str | None
    available_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    run_id: int
    step_id: int | None
    type: str
    payload: dict
    created_at: datetime


class ApprovalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    step_id: int
    status: str
    tool_name: str | None
    arguments: dict
    requested_at: datetime
    decided_at: datetime | None
    decided_by: str | None
    comment: str | None


class ApprovalDecision(BaseModel):
    decided_by: str = "operator"
    comment: str | None = None


class WorkerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    status: str
    concurrency: int
    last_heartbeat_at: datetime


class CostOut(BaseModel):
    run_id: int
    total_tokens: int
    total_cost: float
    by_model: list[dict]
    by_step: list[dict]
