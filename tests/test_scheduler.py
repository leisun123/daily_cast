"""Sprint 2 APScheduler adapter tests."""

from __future__ import annotations

from apscheduler.triggers.cron import CronTrigger

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


def test_scheduler_builds_a_timezone_aware_cron_trigger() -> None:
    """The configured IANA timezone, not the host clock, controls the scheduled edition tick."""
    scheduler = SchedulerService(
        RecordingSubmissionService(),
        lambda: _scheduled_command(),
        cron_expression="15 8 * * *",
        timezone="America/Los_Angeles",
    )

    trigger = scheduler.build_trigger()

    assert isinstance(trigger, CronTrigger)
    assert str(trigger.timezone) == "America/Los_Angeles"
    assert str(trigger.fields[0]) == "*"
    assert str(trigger.fields[5]) == "8"
    assert str(trigger.fields[6]) == "15"


def test_scheduler_isolates_submission_failure() -> None:
    """A failed tick is logged and does not destabilize the FastAPI process or future ticks."""

    class FailingSubmissionService:
        def submit(self, command: TaskCommand) -> None:
            del command
            raise RuntimeError("temporary SQLite failure")

    SchedulerService(FailingSubmissionService(), _scheduled_command).trigger_submission()
    healthy_submissions = RecordingSubmissionService()
    healthy_scheduler = SchedulerService(healthy_submissions, _scheduled_command)

    healthy_scheduler.trigger_submission()

    assert len(healthy_submissions.commands) == 1


def _scheduled_command() -> TaskCommand:
    """Return one deterministic scheduled edition request for the adapter tests."""
    return TaskCommand(
        task_type=TaskType.DAILY_GENERATE,
        request={"edition": "daily", "episode_date": "2026-07-22"},
        config_snapshot={"pipeline": "test"},
        pipeline_version="test-v1",
        trigger_type=TriggerType.SCHEDULED,
    )
