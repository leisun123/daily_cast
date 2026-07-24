"""APScheduler integration kept separate from task execution internals."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable
from typing import Protocol

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from dailycast.pipeline.contracts import TaskCommand

logger = logging.getLogger(__name__)


class SubmissionPort(Protocol):
    """The scheduler's narrow dependency boundary."""

    def submit(self, command: TaskCommand) -> object:
        """Persist and enqueue one durable task request."""


class SchedulerService:
    """Turn a configured cron tick into TaskSubmissionService.submit(command)."""

    def __init__(
        self,
        submission_service: SubmissionPort,
        command_factory: Callable[[], TaskCommand],
        *,
        cron_expression: str = "0 8 * * *",
        timezone: str = "Asia/Shanghai",
        enabled: bool = False,
    ) -> None:
        self._submission_service = submission_service
        self._command_factory = command_factory
        self._cron_expression = cron_expression
        self._timezone = timezone
        self._enabled = enabled
        self._scheduler: AsyncIOScheduler | None = None

    def start(self) -> None:
        """Start APScheduler only outside Uvicorn reload mode and when explicitly enabled."""
        if not self._enabled or self._is_uvicorn_reload():
            return
        if self._scheduler is not None and self._scheduler.running:
            return
        scheduler = AsyncIOScheduler(timezone=self._timezone)
        scheduler.add_job(
            self.trigger_submission,
            trigger=self.build_trigger(),
            id="dailycast-task-submission",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        scheduler.start()
        self._scheduler = scheduler

    def build_trigger(self) -> CronTrigger:
        """Build the timezone-aware daily trigger once for runtime and focused verification."""
        return CronTrigger.from_crontab(self._cron_expression, timezone=self._timezone)

    def shutdown(self) -> None:
        """Stop scheduler ticks before the in-process executor begins graceful shutdown."""
        if self._scheduler is not None and self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def trigger_submission(self) -> None:
        """Submit one command while keeping a failed tick isolated from the application process."""
        try:
            self._submission_service.submit(self._command_factory())
        except Exception:
            logger.exception("scheduled task submission failed")

    @staticmethod
    def _is_uvicorn_reload() -> bool:
        """Disable scheduling in any development reloader process to avoid duplicate jobs."""
        return (
            "--reload" in sys.argv
            or os.environ.get("UVICORN_RELOAD") == "1"
            or os.environ.get("WATCHFILES_RELOADER") == "1"
        )
