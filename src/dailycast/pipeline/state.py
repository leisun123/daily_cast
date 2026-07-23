"""Explicit TaskRun lifecycle validation for the in-process executor."""

from __future__ import annotations

from dailycast.db.models import TaskRunStatus


class TaskStateTransitionError(ValueError):
    """Raised when a caller attempts an undocumented TaskRun status transition."""


_ALLOWED_TRANSITIONS: dict[TaskRunStatus, frozenset[TaskRunStatus]] = {
    TaskRunStatus.QUEUED: frozenset({TaskRunStatus.RUNNING}),
    TaskRunStatus.RUNNING: frozenset(
        {
            TaskRunStatus.SUCCEEDED,
            TaskRunStatus.SUCCEEDED_WITH_WARNINGS,
            TaskRunStatus.WAITING_ACTION,
            TaskRunStatus.FAILED,
            TaskRunStatus.INTERRUPTED,
            TaskRunStatus.CANCELLED,
        }
    ),
    TaskRunStatus.INTERRUPTED: frozenset({TaskRunStatus.QUEUED}),
}


def validate_task_run_transition(current: TaskRunStatus, target: TaskRunStatus) -> None:
    """Reject transitions outside the Sprint 2 TaskRun state machine."""
    if target in _ALLOWED_TRANSITIONS.get(current, frozenset()):
        return
    msg = f"invalid TaskRun transition: {current.value} -> {target.value}"
    raise TaskStateTransitionError(msg)
