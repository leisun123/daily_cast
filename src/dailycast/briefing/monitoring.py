"""Low-cost health probes for the model endpoints used by the daily briefing."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Literal

from pydantic import BaseModel

from dailycast.db.models import LLMOperation
from dailycast.llm.contracts import LLMMessage, LLMProvider

BriefingAlert = Callable[[str, Exception, str | None], Awaitable[None]]


class _ProviderProbeResult(BaseModel):
    """Smallest structured payload that confirms the real generation path works."""

    ok: Literal[True]


class BriefingProviderProbe:
    """Probe every configured direct provider before preparation begins."""

    def __init__(self, providers: Sequence[LLMProvider], *, alert: BriefingAlert) -> None:
        self._providers = tuple(providers)
        self._alert = alert

    async def run(self) -> None:
        """Report each unavailable provider while still checking its configured fallback."""
        for provider in self._providers:
            try:
                result = await provider.generate_structured(
                    operation=LLMOperation.GENERATE_BRIEFING,
                    messages=[
                        LLMMessage(
                            role="user",
                            content='Return only the JSON object {"ok": true}.',
                        )
                    ],
                    response_schema=_ProviderProbeResult,
                    model_options={"max_output_tokens": 16, "temperature": 0.0},
                )
                _ProviderProbeResult.model_validate(result.content)
            except Exception as error:
                await self._alert(f"AI Provider 预检（{provider.provider_name}）", error, None)


__all__ = ["BriefingProviderProbe"]
