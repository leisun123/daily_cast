"""Generic webhook markdown push with per-target payload formats and a bounded retry."""

from __future__ import annotations

import logging
from typing import Literal

import httpx

logger = logging.getLogger(__name__)

WebhookFormat = Literal["wecom_markdown", "wecom_markdown_v2", "generic_json"]
"""How one markdown message maps onto the target's JSON contract.

`wecom_markdown` and `wecom_markdown_v2` speak the respective WeCom
group-robot envelopes and treat a non-zero `errcode` as a failure;
`generic_json` posts a text envelope and treats any HTTP 200 as success.
"""


class WebhookPushError(RuntimeError):
    """The webhook endpoint rejected or failed one markdown push."""


class WebhookNotifier:
    """Push one markdown message to any configured JSON webhook target."""

    def __init__(
        self,
        webhook_url: str,
        *,
        payload_format: WebhookFormat = "wecom_markdown",
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 10.0,
        max_attempts: int = 2,
    ) -> None:
        self._webhook_url = webhook_url
        self._payload_format = payload_format
        self._client = client
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts

    async def push(self, markdown: str) -> None:
        """Send one rendered payload, retrying once before reporting failure."""
        payload = self._render_payload(markdown)
        last_error: WebhookPushError | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                await self._send(payload)
            except WebhookPushError as error:
                last_error = error
                logger.warning(
                    "webhook push attempt failed",
                    extra={"attempt": attempt, "error": str(error)},
                )
            else:
                return
        assert last_error is not None
        raise last_error

    def _render_payload(self, markdown: str) -> dict[str, object]:
        """Map the markdown onto the payload contract the target expects."""
        if self._payload_format == "wecom_markdown":
            return {"msgtype": "markdown", "markdown": {"content": markdown}}
        if self._payload_format == "wecom_markdown_v2":
            return {"msgtype": "markdown_v2", "markdown_v2": {"content": markdown}}
        return {"text": markdown}

    async def _send(self, payload: dict[str, object]) -> None:
        """Deliver one payload and apply the target's success criteria."""
        try:
            if self._client is not None:
                response = await self._client.post(
                    self._webhook_url, json=payload, timeout=self._timeout_seconds
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                    response = await client.post(self._webhook_url, json=payload)
        except httpx.HTTPError as error:
            raise WebhookPushError(f"webhook request failed: {error.__class__.__name__}") from error
        if response.status_code != 200:
            raise WebhookPushError(f"webhook returned HTTP {response.status_code}")
        if self._payload_format not in {"wecom_markdown", "wecom_markdown_v2"}:
            # Generic targets own their response body; a 200 means delivered.
            return
        try:
            body = response.json()
        except ValueError as error:
            raise WebhookPushError("webhook returned a non-JSON body") from error
        errcode = body.get("errcode")
        if errcode != 0:
            raise WebhookPushError(
                f"webhook rejected the message: errcode={errcode} errmsg={body.get('errmsg')}"
            )
