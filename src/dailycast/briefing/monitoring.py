"""Minimal provider health probe run before the daily briefing generation."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel

from dailycast.briefing.alerts import BriefingAlert
from dailycast.db.models import LLMOperation
from dailycast.llm.contracts import LLMMessage, LLMProvider

# Reasoning models bill their thinking tokens against the output budget before
# any JSON appears (deepseek-v4-pro burns 16+ tokens reasoning about even this
# one-line prompt), so the probe must leave room for a full chain of thought.
_PROBE_MAX_OUTPUT_TOKENS = 1024
_PROBE_TIMEOUT_SECONDS = 30.0


class _ProbeOK(BaseModel):
    """Smallest structured payload that confirms the real generation path works."""

    ok: Literal[True]


async def _probe(provider: LLMProvider) -> None:
    result = await provider.generate_structured(
        operation=LLMOperation.GENERATE_BRIEFING,
        messages=[LLMMessage(role="user", content='Return only the JSON object {"ok": true}.')],
        response_schema=_ProbeOK,
        model_options={"max_output_tokens": _PROBE_MAX_OUTPUT_TOKENS, "temperature": 0.0},
    )
    _ProbeOK.model_validate(result.content)


async def preflight_providers(
    providers: Sequence[LLMProvider],
    alert: BriefingAlert,
    *,
    probe_timeout_seconds: float = _PROBE_TIMEOUT_SECONDS,
) -> None:
    """Ping every configured provider concurrently with one tiny structured request."""
    outcomes = await asyncio.gather(
        *(
            asyncio.wait_for(_probe(provider), timeout=probe_timeout_seconds)
            for provider in providers
        ),
        return_exceptions=True,
    )
    for provider, outcome in zip(providers, outcomes, strict=True):
        if isinstance(outcome, Exception):
            await alert(f"AI Provider 预检（{provider.provider_name}）", outcome)


__all__ = ["preflight_providers"]
