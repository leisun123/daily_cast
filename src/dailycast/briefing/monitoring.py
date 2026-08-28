"""Minimal provider health probe run before the daily briefing generation."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel

from dailycast.briefing.alerts import BriefingAlert
from dailycast.db.models import LLMOperation
from dailycast.llm.contracts import LLMMessage, LLMProvider


class _ProbeOK(BaseModel):
    """Smallest structured payload that confirms the real generation path works."""

    ok: Literal[True]


async def preflight_providers(providers: Sequence[LLMProvider], alert: BriefingAlert) -> None:
    """Ping every configured provider with one tiny structured request."""
    for provider in providers:
        try:
            result = await provider.generate_structured(
                operation=LLMOperation.GENERATE_BRIEFING,
                messages=[
                    LLMMessage(role="user", content='Return only the JSON object {"ok": true}.')
                ],
                response_schema=_ProbeOK,
                model_options={"max_output_tokens": 16, "temperature": 0.0},
            )
            _ProbeOK.model_validate(result.content)
        except Exception as error:
            await alert(f"AI Provider 预检（{provider.provider_name}）", error)


__all__ = ["preflight_providers"]
