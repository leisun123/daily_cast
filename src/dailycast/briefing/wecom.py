"""WeCom group-bot markdown push with a bounded single retry."""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


class WeComPushError(RuntimeError):
    """The WeCom webhook rejected or failed one markdown push."""


class WeComNotifier:
    """Push one markdown message to a configured WeCom group robot webhook."""

    def __init__(
        self,
        webhook_url: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 10.0,
        max_attempts: int = 2,
    ) -> None:
        self._webhook_url = webhook_url
        self._client = client
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts

    async def push(self, markdown: str) -> None:
        """Send one markdown payload, retrying once before reporting failure."""
        payload: dict[str, object] = {"msgtype": "markdown", "markdown": {"content": markdown}}
        last_error: WeComPushError | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                await self._send(payload)
            except WeComPushError as error:
                last_error = error
                logger.warning(
                    "wecom push attempt failed",
                    extra={"attempt": attempt, "error": str(error)},
                )
            else:
                return
        assert last_error is not None
        raise last_error

    async def _send(self, payload: dict[str, object]) -> None:
        """Deliver one payload and treat any non-zero errcode as a push failure."""
        try:
            if self._client is not None:
                response = await self._client.post(
                    self._webhook_url, json=payload, timeout=self._timeout_seconds
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                    response = await client.post(self._webhook_url, json=payload)
        except httpx.HTTPError as error:
            raise WeComPushError(
                f"wecom webhook request failed: {error.__class__.__name__}"
            ) from error
        if response.status_code != 200:
            raise WeComPushError(f"wecom webhook returned HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as error:
            raise WeComPushError("wecom webhook returned a non-JSON body") from error
        errcode = body.get("errcode")
        if errcode != 0:
            raise WeComPushError(
                f"wecom webhook rejected the message: errcode={errcode} "
                f"errmsg={body.get('errmsg')}"
            )
