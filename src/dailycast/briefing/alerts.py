"""Best-effort Enterprise WeChat alerts for DailyCast briefing failures."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from dailycast.briefing.webhook import WebhookNotifier

logger = logging.getLogger(__name__)


class BriefingAlertReporter:
    """Send a compact operational alert without changing the failed briefing outcome."""

    def __init__(
        self,
        notifier: WebhookNotifier | None,
        *,
        now: Callable[[], datetime],
        timezone: str,
    ) -> None:
        self._notifier = notifier
        self._now = now
        self._timezone = ZoneInfo(timezone)

    async def report(
        self,
        *,
        stage: str,
        error: Exception | str,
        briefing_date: str | None = None,
    ) -> None:
        """Best-effort push that never replaces the original briefing failure."""
        if self._notifier is None:
            return
        try:
            await self._notifier.push(
                self._render(stage=stage, error=error, briefing_date=briefing_date)
            )
        except Exception:
            logger.exception("briefing alert webhook push failed", extra={"stage": stage})

    def _render(
        self,
        *,
        stage: str,
        error: Exception | str,
        briefing_date: str | None,
    ) -> str:
        triggered_at = self._now().astimezone(self._timezone)
        lines = [
            "# DailyCast 异常告警",
            "",
            f"- 环节：{stage}",
            f"- 时间：{triggered_at:%Y-%m-%d %H:%M}（{self._timezone.key}）",
        ]
        if briefing_date is not None:
            lines.append(f"- 报告日期：{briefing_date}")
        lines.append(f"- 错误：{_error_summary(error)}")
        return "\n".join(lines)


def _error_summary(error: Exception | str) -> str:
    """Keep one actionable, single-line error summary suitable for a group alert."""
    if isinstance(error, Exception):
        label = error.__class__.__name__
        detail = str(error)
        return label if not detail else f"{label}: {_single_line(detail)}"
    return _single_line(error)


def _single_line(value: str) -> str:
    """Avoid a remote error body turning one alert into an oversized message."""
    return " ".join(value.split())[:240]


__all__ = ["BriefingAlertReporter"]
