"""Best-effort Enterprise WeChat alerts for DailyCast briefing failures."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx

from dailycast.briefing.webhook import WebhookFormat, WebhookNotifier

logger = logging.getLogger(__name__)

BriefingAlert = Callable[[str, Exception], Awaitable[None]]


def build_alert(
    webhook_url: str,
    *,
    payload_format: WebhookFormat = "wecom_markdown_v2",
    timezone: str = "Asia/Shanghai",
    now: Callable[[], datetime] | None = None,
    client: httpx.AsyncClient | None = None,
) -> BriefingAlert:
    """Build one ``alertmsg(stage, error)`` that never disturbs the briefing flow."""
    notifier = WebhookNotifier(webhook_url, payload_format=payload_format, client=client)
    clock = now or (lambda: datetime.now(UTC))
    tz = ZoneInfo(timezone)

    async def alertmsg(stage: str, error: Exception) -> None:
        triggered_at = clock().astimezone(tz)
        markdown = "\n".join(
            (
                "# DailyCast 异常告警",
                "",
                f"- 环节：{stage}",
                f"- 时间：{triggered_at:%Y-%m-%d %H:%M}（{tz.key}）",
                f"- 错误：{_error_summary(error)}",
            )
        )
        try:
            await notifier.push(markdown)
        except Exception:
            logger.exception("briefing alert webhook push failed", extra={"stage": stage})

    return alertmsg


def _error_summary(error: Exception) -> str:
    """Keep one actionable, single-line error summary suitable for a group alert."""
    label = error.__class__.__name__
    detail = str(error)
    return label if not detail else f"{label}: {_single_line(detail)}"


def _single_line(value: str) -> str:
    """Avoid a remote error body turning one alert into an oversized message."""
    return " ".join(value.split())[:240]


__all__ = ["BriefingAlert", "build_alert"]
