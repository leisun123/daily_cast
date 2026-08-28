"""Enterprise WeChat alert behavior for the scheduled text briefing."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import httpx
from pydantic import BaseModel

from dailycast.briefing.alerts import BriefingAlertReporter
from dailycast.briefing.monitoring import BriefingProviderProbe
from dailycast.briefing.webhook import WebhookNotifier
from dailycast.llm.contracts import LLMMessage, LLMUsage, StructuredResult

ALERT_WEBHOOK_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=alert-test-key"


def test_alert_reports_stage_time_and_error_through_its_own_wecom_webhook() -> None:
    """An operator can identify the failed briefing stage without opening service logs."""
    sent: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    reporter = BriefingAlertReporter(
        WebhookNotifier(
            ALERT_WEBHOOK_URL,
            payload_format="wecom_markdown_v2",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ),
        now=lambda: datetime(2026, 8, 28, 7, 55, tzinfo=UTC),
        timezone="Asia/Shanghai",
    )

    asyncio.run(
        reporter.report(
            stage="消息生成",
            error=RuntimeError("provider timed out"),
            briefing_date="2026-08-27",
        )
    )

    assert sent == [
        {
            "msgtype": "markdown_v2",
            "markdown_v2": {
                "content": (
                    "# DailyCast 异常告警\n\n"
                    "- 环节：消息生成\n"
                    "- 时间：2026-08-28 15:55（Asia/Shanghai）\n"
                    "- 报告日期：2026-08-27\n"
                    "- 错误：RuntimeError: provider timed out"
                )
            },
        }
    ]


def test_alert_delivery_failure_does_not_replace_the_original_briefing_failure() -> None:
    """A broken alert bot is logged only; it must never break the scheduled briefing flow."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(500)

    reporter = BriefingAlertReporter(
        WebhookNotifier(
            ALERT_WEBHOOK_URL,
            payload_format="wecom_markdown_v2",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ),
        now=lambda: datetime(2026, 8, 28, 7, 55, tzinfo=UTC),
        timezone="Asia/Shanghai",
    )

    asyncio.run(reporter.report(stage="企业微信发送", error=RuntimeError("main webhook failed")))


def test_provider_preflight_alerts_only_the_provider_that_cannot_make_a_real_request() -> None:
    """A healthy fallback does not hide an unavailable preferred provider from operators."""
    alerts: list[tuple[str, str]] = []
    primary = _ProbeProvider("primary", error=RuntimeError("rate limited"))
    fallback = _ProbeProvider("fallback")

    async def alert(stage: str, error: Exception, briefing_date: str | None) -> None:
        assert briefing_date is None
        alerts.append((stage, str(error)))

    asyncio.run(BriefingProviderProbe((primary, fallback), alert=alert).run())

    assert primary.calls == 1
    assert fallback.calls == 1
    assert alerts == [("AI Provider 预检（primary）", "rate limited")]


def test_provider_preflight_alerts_when_the_structured_response_is_invalid() -> None:
    """An HTTP-successful endpoint is unhealthy when it cannot satisfy the briefing schema."""
    alerts: list[tuple[str, str]] = []

    async def alert(stage: str, error: Exception, briefing_date: str | None) -> None:
        assert briefing_date is None
        alerts.append((stage, error.__class__.__name__))

    asyncio.run(
        BriefingProviderProbe(
            (_ProbeProvider("primary", payload={"ok": False}),), alert=alert
        ).run()
    )

    assert alerts == [("AI Provider 预检（primary）", "ValidationError")]


class _ProbeResponse(BaseModel):
    ok: bool


class _ProbeProvider:
    """Minimal structured provider double; only external network transport is omitted."""

    max_output_tokens = 32
    model = "test-model"

    def __init__(
        self,
        provider_name: str,
        *,
        error: Exception | None = None,
        payload: dict[str, bool] | None = None,
    ) -> None:
        self.provider_name = provider_name
        self._error = error
        self._payload = payload
        self.calls = 0

    def generation_config_hash(self, model_options: object) -> str:
        del model_options
        return "test"

    async def generate_structured(
        self,
        operation: object,
        messages: list[LLMMessage],
        response_schema: type[BaseModel],
        model_options: object,
    ) -> StructuredResult:
        del operation, messages, response_schema, model_options
        self.calls += 1
        if self._error is not None:
            raise self._error
        return StructuredResult(
            content=self._payload or _ProbeResponse(ok=True).model_dump(),
            model=self.model,
            usage=LLMUsage(),
            request_id=None,
        )
