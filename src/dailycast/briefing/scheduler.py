"""Cron adapter that turns a configured tick into one briefing run."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from dailycast.briefing.service import BriefingRunInProgressError, BriefingRunReport

logger = logging.getLogger(__name__)


class BriefingScheduler:
    """Turn a configured cron tick into one isolated BriefingService.run() call."""

    def __init__(
        self,
        run: Callable[[], Awaitable[BriefingRunReport]],
        *,
        cron_expression: str,
        timezone: str,
    ) -> None:
        self._run = run
        self._cron_expression = cron_expression
        self._timezone = timezone
        self._scheduler: AsyncIOScheduler | None = None

    def start(self) -> None:
        """Start APScheduler only outside Uvicorn reload mode."""
        if self._is_uvicorn_reload():
            return
        if self._scheduler is not None and self._scheduler.running:
            return
        scheduler = AsyncIOScheduler(timezone=self._timezone)
        scheduler.add_job(
            self.trigger_run,
            trigger=self.build_trigger(),
            id="dailycast-briefing-run",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        scheduler.start()
        self._scheduler = scheduler

    def build_trigger(self) -> CronTrigger:
        """Build the timezone-aware cron trigger once for runtime and focused verification."""
        return CronTrigger.from_crontab(self._cron_expression, timezone=self._timezone)

    def shutdown(self) -> None:
        """Stop briefing ticks before the application begins graceful shutdown."""
        if self._scheduler is not None and self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    async def trigger_run(self) -> None:
        """Run one briefing while keeping a failed tick isolated from the process."""
        try:
            await self._run()
        except BriefingRunInProgressError:
            # A manual run overlapping the cron tick is normal operation, not a failure.
            logger.info("scheduled briefing skipped: a run is already in progress")
        except Exception:
            logger.exception("scheduled briefing run failed")

    @staticmethod
    def _is_uvicorn_reload() -> bool:
        """Disable scheduling in any development reloader process to avoid duplicate jobs."""
        return (
            "--reload" in sys.argv
            or os.environ.get("UVICORN_RELOAD") == "1"
            or os.environ.get("WATCHFILES_RELOADER") == "1"
        )
