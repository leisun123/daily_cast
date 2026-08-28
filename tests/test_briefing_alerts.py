"""Enterprise WeChat alert behavior for the scheduled text briefing."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import httpx
from pydantic import BaseModel

from dailycast.briefing.alerts import build_alert
from dailycast.briefing.monitoring import preflight_providers
from dailycast.llm.contracts import LLMMessage, LLMUsage, StructuredResult

ALERT_WEBHOOK_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=alert-test-key"


def test_alertmsg_pushes_stage_time_and_error_to_the_alert_robot() -> None:
    """An operator can identify the failed briefing stage without opening service logs."""
    sent: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    alertmsg = build_alert(
        ALERT_WEBHOOK_URL,
        timezone="Asia/Shanghai",
        now=lambda: datetime(2026, 8, 28, 7, 55, tzinfo=UTC),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    asyncio.run(alertmsg("消息生成", RuntimeError("provider timed out")))

    assert sent == [
        {
            "msgtype": "markdown_v2",
            "markdown_v2": {
                "content": (
                    "# DailyCast 异常告警\n\n"
                    "- 环节：消息生成\n"
                    "- 时间：2026-08-28 15:55（Asia/Shanghai）\n"
                    "- 错误：RuntimeError: provider timed out"
                )
            },
        }
    ]


def test_alertmsg_never_raises_when_the_alert_robot_itself_is_down() -> None:
    """A broken alert bot is logged only; it must never break the briefing flow."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(500)

    alertmsg = build_alert(
        ALERT_WEBHOOK_URL,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    asyncio.run(alertmsg("企业微信发送", RuntimeError("main webhook failed")))


def test_provider_preflight_alerts_only_the_provider_that_cannot_answer() -> None:
    """A healthy fallback does not hide an unavailable preferred provider from operators."""
    alerts: list[tuple[str, str]] = []
    primary = _ProbeProvider("primary", error=RuntimeError("rate limited"))
    fallback = _ProbeProvider("fallback")

    async def alertmsg(stage: str, error: Exception) -> None:
        alerts.append((stage, str(error)))

    asyncio.run(preflight_providers((primary, fallback), alertmsg))

    assert primary.calls == 1
    assert fallback.calls == 1
    assert alerts == [("AI Provider 预检（primary）", "rate limited")]


def test_provider_preflight_is_silent_when_every_provider_answers() -> None:
    """A normal morning sends nothing through the alert robot."""
    alerts: list[tuple[str, str]] = []

    async def alertmsg(stage: str, error: Exception) -> None:
        alerts.append((stage, str(error)))

    asyncio.run(
        preflight_providers(
            (_ProbeProvider("primary"), _ProbeProvider("fallback")), alertmsg
        )
    )

    assert alerts == []


def test_provider_preflight_reports_a_hung_provider_instead_of_waiting() -> None:
    """One wedged endpoint cannot stall the 07:55 generation start."""
    alerts: list[tuple[str, str]] = []

    async def alertmsg(stage: str, error: Exception) -> None:
        alerts.append((stage, error.__class__.__name__))

    asyncio.run(
        preflight_providers(
            (_ProbeProvider("hung", delay=10.0),),
            alertmsg,
            probe_timeout_seconds=0.01,
        )
    )

    assert alerts == [("AI Provider 预检（hung）", "TimeoutError")]


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
        delay: float = 0.0,
    ) -> None:
        self.provider_name = provider_name
        self._error = error
        self._delay = delay
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
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return StructuredResult(
            content=_ProbeResponse(ok=True).model_dump(),
            model=self.model,
            usage=LLMUsage(),
            request_id=None,
        )
