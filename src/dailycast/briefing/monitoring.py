"""Provider reachability probes run before the daily briefing generation."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Protocol

from dailycast.briefing.alerts import BriefingAlert

# Providers cap each of their two probe attempts at 60s (plus a short pause);
# the outer bound only guards a wedged transport that never returns.
_PROBE_TIMEOUT_SECONDS = 150.0


class PingableProvider(Protocol):
    """A direct provider able to confirm reachability without generating."""

    provider_name: str

    async def ping(self) -> None: ...


async def preflight_providers(
    providers: Sequence[PingableProvider],
    alert: BriefingAlert,
    *,
    probe_timeout_seconds: float = _PROBE_TIMEOUT_SECONDS,
) -> None:
    """Check every configured provider concurrently; report the ones that fail."""
    outcomes = await asyncio.gather(
        *(
            asyncio.wait_for(provider.ping(), timeout=probe_timeout_seconds)
            for provider in providers
        ),
        return_exceptions=True,
    )
    for provider, outcome in zip(providers, outcomes, strict=True):
        if isinstance(outcome, Exception):
            await alert(f"AI Provider 预检（{provider.provider_name}）", outcome)


__all__ = ["preflight_providers"]
