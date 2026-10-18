import pytest

from app.state_machine import (
    InvalidTransition,
    RunStatus,
    StepStatus,
    ensure_run_transition,
    ensure_step_transition,
)


def test_valid_transitions():
    ensure_run_transition(RunStatus.PENDING, RunStatus.RUNNING)
    ensure_run_transition(RunStatus.RUNNING, RunStatus.WAITING_APPROVAL)
    ensure_run_transition(RunStatus.WAITING_APPROVAL, RunStatus.RUNNING)
    ensure_step_transition(StepStatus.PENDING, StepStatus.RUNNING)
    ensure_step_transition(StepStatus.RUNNING, StepStatus.RETRYING)
    ensure_step_transition(StepStatus.RETRYING, StepStatus.RUNNING)
    ensure_step_transition(StepStatus.FAILED, StepStatus.PENDING)


def test_invalid_transition():
    with pytest.raises(InvalidTransition):
        ensure_run_transition(RunStatus.SUCCEEDED, RunStatus.RUNNING)
    with pytest.raises(InvalidTransition):
        ensure_step_transition(StepStatus.SUCCEEDED, StepStatus.RUNNING)
