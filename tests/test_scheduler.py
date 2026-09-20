"""Sprint 2 APScheduler adapter tests."""

from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

from dailycast.briefing.scheduler import BriefingScheduler
from dailycast.briefing.service import BriefingRunInProgressError, BriefingRunReport
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


def _always_workday(_: object) -> bool:
    return True


def _briefing_scheduler(
    prepare: object,
    deliver: object,
    *,
    alert: object = None,
    preflight: object = None,
    is_working_day: object = None,
    skip_non_working_days: bool = True,
    now: object = None,
) -> BriefingScheduler:
    return BriefingScheduler(
        prepare,
        deliver,
        preparation_cron_expression="55 7 * * *",
        preparation_retry_cron_expression="15 8 * * *",
        delivery_cron_expression="30 8 * * *",
        timezone="Asia/Shanghai",
        alert=alert,
        preflight=preflight,
        is_working_day=is_working_day or _always_workday,
        skip_non_working_days=skip_non_working_days,
        now=now,
    )


def test_briefing_scheduler_prepares_before_the_delivery_tick() -> None:
    """The 08:30 tick sends a ready briefing rather than starting generation."""
    actions: list[str] = []
    report = BriefingRunReport(date="2026-08-25", categories=())

    async def prepare() -> BriefingRunReport:
        actions.append("prepare")
        return report

    async def deliver() -> BriefingRunReport:
        actions.append("deliver")
        return report

    scheduler = _briefing_scheduler(prepare, deliver)

    asyncio.run(scheduler.trigger_prepare())
    asyncio.run(scheduler.trigger_delivery())

    assert actions == ["prepare", "deliver"]
    assert str(scheduler.build_preparation_trigger().fields[5]) == "7"
    assert str(scheduler.build_preparation_trigger().fields[6]) == "55"
    assert str(scheduler.build_delivery_trigger().fields[5]) == "8"
    assert str(scheduler.build_delivery_trigger().fields[6]) == "30"
    assert str(scheduler.build_preparation_trigger().fields[4]) == "*"


def test_briefing_scheduler_skips_non_working_days() -> None:
    """A makeup-free Sunday tick must not generate or deliver the briefing."""
    actions: list[str] = []
    report = BriefingRunReport(date="2026-09-19", categories=())

    async def prepare() -> BriefingRunReport:
        actions.append("prepare")
        return report

    async def deliver() -> BriefingRunReport:
        actions.append("deliver")
        return report

    sunday = datetime(2026, 9, 19, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    scheduler = _briefing_scheduler(
        prepare,
        deliver,
        is_working_day=lambda day: day.weekday() < 5,
        now=lambda: sunday,
    )

    asyncio.run(scheduler.trigger_prepare())
    asyncio.run(scheduler.trigger_delivery())

    assert actions == []


def test_briefing_scheduler_runs_on_makeup_workdays() -> None:
    """Statutory makeup workdays such as 调休 still send the briefing."""
    actions: list[str] = []
    report = BriefingRunReport(date="2026-09-20", categories=())

    async def prepare() -> BriefingRunReport:
        actions.append("prepare")
        return report

    async def deliver() -> BriefingRunReport:
        actions.append("deliver")
        return report

    makeup_sunday = datetime(2026, 9, 20, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    scheduler = _briefing_scheduler(
        prepare,
        deliver,
        is_working_day=lambda day: day.isoformat() == "2026-09-20" or day.weekday() < 5,
        now=lambda: makeup_sunday,
    )

    asyncio.run(scheduler.trigger_prepare())
    asyncio.run(scheduler.trigger_delivery())

    assert actions == ["prepare", "deliver"]


def test_briefing_scheduler_alerts_when_preparation_raises() -> None:
    """A collection or generation exception reaches the independent alert path."""
    alerts: list[tuple[str, str]] = []

    async def prepare() -> BriefingRunReport:
        raise RuntimeError("collection failed")

    async def deliver() -> BriefingRunReport:
        return BriefingRunReport(date="2026-08-27", categories=())

    async def alert(stage: str, error: Exception) -> None:
        alerts.append((stage, str(error)))

    scheduler = _briefing_scheduler(prepare, deliver, alert=alert)

    asyncio.run(scheduler.trigger_prepare())

    assert alerts == [("消息生成", "collection failed")]


def test_briefing_scheduler_alerts_when_delivery_raises() -> None:
    """A scheduler-level delivery failure is reported when it escapes the service."""
    alerts: list[tuple[str, str]] = []

    async def prepare() -> BriefingRunReport:
        return BriefingRunReport(date="2026-08-27", categories=())

    async def deliver() -> BriefingRunReport:
        raise RuntimeError("delivery crashed")

    async def alert(stage: str, error: Exception) -> None:
        alerts.append((stage, str(error)))

    scheduler = _briefing_scheduler(prepare, deliver, alert=alert)

    asyncio.run(scheduler.trigger_delivery())

    assert alerts == [("企业微信发送", "delivery crashed")]


def test_briefing_scheduler_does_not_alert_when_delivery_hits_an_in_progress_run() -> None:
    """A slow 08:15 prepare overlapping the 08:30 tick is not a delivery failure."""
    alerts: list[tuple[str, str]] = []

    async def prepare() -> BriefingRunReport:
        return BriefingRunReport(date="2026-08-27", categories=())

    async def deliver() -> BriefingRunReport:
        raise BriefingRunInProgressError("briefing run already in progress")

    async def alert(stage: str, error: Exception) -> None:
        alerts.append((stage, str(error)))

    scheduler = _briefing_scheduler(prepare, deliver, alert=alert)

    asyncio.run(scheduler.trigger_delivery())

    assert alerts == []


def test_briefing_scheduler_probes_providers_before_preparation() -> None:
    """The provider ping runs just before generation on the same prepare tick."""
    actions: list[str] = []

    async def prepare() -> BriefingRunReport:
        actions.append("prepare")
        return BriefingRunReport(date="2026-08-27", categories=())

    async def deliver() -> BriefingRunReport:
        actions.append("deliver")
        return BriefingRunReport(date="2026-08-27", categories=())

    async def preflight() -> None:
        actions.append("preflight")

    scheduler = _briefing_scheduler(prepare, deliver, preflight=preflight)

    asyncio.run(scheduler.trigger_prepare())
    asyncio.run(scheduler.trigger_delivery())

    assert actions == ["preflight", "prepare", "deliver"]


def test_briefing_scheduler_keeps_preparing_when_the_probe_itself_crashes() -> None:
    """A broken probe must never block the briefing it was meant to guard."""
    actions: list[str] = []

    async def prepare() -> BriefingRunReport:
        actions.append("prepare")
        return BriefingRunReport(date="2026-08-27", categories=())

    async def deliver() -> BriefingRunReport:
        return BriefingRunReport(date="2026-08-27", categories=())

    async def preflight() -> None:
        raise RuntimeError("probe crashed")

    scheduler = _briefing_scheduler(prepare, deliver, preflight=preflight)

    asyncio.run(scheduler.trigger_prepare())

    assert actions == ["prepare"]


def _scheduled_command() -> TaskCommand:
    """Return one deterministic scheduled edition request for the adapter tests."""
    return TaskCommand(
        task_type=TaskType.DAILY_GENERATE,
        request={"edition": "daily", "episode_date": "2026-07-22"},
        config_snapshot={"pipeline": "test"},
        pipeline_version="test-v1",
        trigger_type=TriggerType.SCHEDULED,
    )
