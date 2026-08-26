"""Cron adapter that prepares a briefing before the delivery tick."""

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
    """Prepare early, then make the 08:30 tick a fast delivery-only operation."""

    def __init__(
        self,
        prepare: Callable[[], Awaitable[BriefingRunReport]],
        deliver: Callable[[], Awaitable[BriefingRunReport]],
        *,
        preparation_cron_expression: str,
        preparation_retry_cron_expression: str,
        delivery_cron_expression: str,
        timezone: str,
    ) -> None:
        self._prepare = prepare
        self._deliver = deliver
        self._preparation_cron_expression = preparation_cron_expression
        self._preparation_retry_cron_expression = preparation_retry_cron_expression
        self._delivery_cron_expression = delivery_cron_expression
        self._timezone = timezone
        self._scheduler: AsyncIOScheduler | None = None

    def start(self) -> None:
        """Start APScheduler only outside Uvicorn reload mode."""
        if self._is_uvicorn_reload():
            return
        if self._scheduler is not None and self._scheduler.running:
            return
        scheduler = AsyncIOScheduler(timezone=self._timezone)
        for job_id, callback, trigger in (
            (
                "dailycast-briefing-prepare",
                self.trigger_prepare,
                self.build_preparation_trigger(),
            ),
            (
                "dailycast-briefing-prepare-retry",
                self.trigger_prepare,
                self.build_preparation_retry_trigger(),
            ),
            (
                "dailycast-briefing-deliver",
                self.trigger_delivery,
                self.build_delivery_trigger(),
            ),
        ):
            scheduler.add_job(
                callback,
                trigger=trigger,
                id=job_id,
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
        scheduler.start()
        self._scheduler = scheduler

    def build_preparation_trigger(self) -> CronTrigger:
        """Build the first early preparation trigger for the daily report."""
        return CronTrigger.from_crontab(self._preparation_cron_expression, timezone=self._timezone)

    def build_preparation_retry_trigger(self) -> CronTrigger:
        """Build the retry trigger used only when the early report is not ready."""
        return CronTrigger.from_crontab(
            self._preparation_retry_cron_expression, timezone=self._timezone
        )

    def build_delivery_trigger(self) -> CronTrigger:
        """Build the delivery trigger; it only reads and posts prepared markdown."""
        return CronTrigger.from_crontab(self._delivery_cron_expression, timezone=self._timezone)

    def build_trigger(self) -> CronTrigger:
        """Keep the legacy helper as an alias for the actual delivery time."""
        return self.build_delivery_trigger()

    def shutdown(self) -> None:
        """Stop briefing ticks before the application begins graceful shutdown."""
        if self._scheduler is not None and self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    async def trigger_prepare(self) -> None:
        """Prepare one briefing while keeping a failed tick isolated from the process."""
        try:
            await self._prepare()
        except BriefingRunInProgressError:
            logger.info("scheduled briefing preparation skipped: a run is already in progress")
        except Exception:
            logger.exception("scheduled briefing preparation failed")

    async def trigger_delivery(self) -> None:
        """Deliver the already-persisted briefing without any collection or LLM work."""
        try:
            await self._deliver()
        except Exception:
            logger.exception("scheduled briefing delivery failed")

    @staticmethod
    def _is_uvicorn_reload() -> bool:
        """Disable scheduling in any development reloader process to avoid duplicate jobs."""
        return (
            "--reload" in sys.argv
            or os.environ.get("UVICORN_RELOAD") == "1"
            or os.environ.get("WATCHFILES_RELOADER") == "1"
        )
