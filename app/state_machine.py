from enum import StrEnum


class RunStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class StepStatus(StrEnum):
    BLOCKED = "BLOCKED"
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    RETRYING = "RETRYING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    DEAD = "DEAD"
    CANCELED = "CANCELED"


TERMINAL_RUN_STATUSES = {
    RunStatus.SUCCEEDED,
    RunStatus.FAILED,
    RunStatus.CANCELED,
}
TERMINAL_STEP_STATUSES = {
    StepStatus.SUCCEEDED,
    StepStatus.FAILED,
    StepStatus.DEAD,
    StepStatus.CANCELED,
}

_RUN_TRANSITIONS: dict[str, set[str]] = {
    RunStatus.PENDING: {RunStatus.RUNNING, RunStatus.WAITING_APPROVAL, RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELED},
    RunStatus.RUNNING: {RunStatus.WAITING_APPROVAL, RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELED},
    RunStatus.WAITING_APPROVAL: {RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELED},
    RunStatus.SUCCEEDED: set(),
    RunStatus.FAILED: set(),
    RunStatus.CANCELED: set(),
}

_STEP_TRANSITIONS: dict[str, set[str]] = {
    StepStatus.BLOCKED: {StepStatus.PENDING, StepStatus.CANCELED},
    StepStatus.PENDING: {StepStatus.RUNNING, StepStatus.CANCELED},
    StepStatus.RUNNING: {
        StepStatus.SUCCEEDED,
        StepStatus.RETRYING,
        StepStatus.WAITING_APPROVAL,
        StepStatus.FAILED,
        StepStatus.DEAD,
        StepStatus.PENDING,  # 租约过期后重新入队
        StepStatus.CANCELED,
    },
    StepStatus.RETRYING: {StepStatus.RUNNING, StepStatus.DEAD, StepStatus.CANCELED},
    StepStatus.WAITING_APPROVAL: {StepStatus.PENDING, StepStatus.FAILED, StepStatus.CANCELED},
    StepStatus.SUCCEEDED: set(),
    StepStatus.FAILED: {StepStatus.PENDING},  # 手动重试
    StepStatus.DEAD: {StepStatus.PENDING},  # 手动重试
    StepStatus.CANCELED: set(),
}


class InvalidTransition(ValueError):
    pass


def ensure_run_transition(current: str, new: str) -> None:
    if new not in _RUN_TRANSITIONS.get(current, set()):
        raise InvalidTransition(f"非法运行状态迁移: {current} -> {new}")


def ensure_step_transition(current: str, new: str) -> None:
    if new not in _STEP_TRANSITIONS.get(current, set()):
        raise InvalidTransition(f"非法步骤状态迁移: {current} -> {new}")


def can_claim(status: str) -> bool:
    return status in {StepStatus.PENDING, StepStatus.RETRYING}
