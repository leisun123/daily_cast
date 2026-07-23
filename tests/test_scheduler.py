"""Sprint 2 APScheduler adapter tests."""

from __future__ import annotations

from dailycast.db.models import TaskType, TriggerType
from dailycast.pipeline.contracts import TaskCommand
from dailycast.scheduler.service import SchedulerService


class RecordingSubmissionService:
    """Capture scheduler submissions without an executor or database."""

    def __init__(self) -> None:
        self.commands: list[TaskCommand] = []

    def submit(self, command: TaskCommand) -> None:
        """Record the command received from the scheduler adapter."""
        self.commands.append(command)


def test_scheduler_submits_command_through_submission_service() -> None:
    """The scheduler builds a scheduled command without calling an executor directly."""
    submissions = RecordingSubmissionService()
    scheduler = SchedulerService(
        submissions,
        lambda: TaskCommand(
            task_type=TaskType.DAILY_GENERATE,
            request={"edition": "daily", "episode_date": "2026-07-22"},
            config_snapshot={"pipeline": "test"},
            pipeline_version="test-v1",
            trigger_type=TriggerType.SCHEDULED,
        ),
        enabled=False,
    )

    scheduler.trigger_submission()

    assert len(submissions.commands) == 1
    assert submissions.commands[0].trigger_type == TriggerType.SCHEDULED
