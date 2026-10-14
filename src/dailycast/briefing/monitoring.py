"""Provider reachability probes run before the daily briefing generation."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Protocol

from dailycast.briefing.alerts import BriefingAlert

logger = logging.getLogger(__name__)
# Providers cap each of their two probe attempts at 60s (plus a short pause);
# the outer bound only guards a wedged transport that never returns.
_PROBE_TIMEOUT_SECONDS = 150.0


class PingableProvider(Protocol):
    """A direct provider able to confirm reachability without generating."""

    provider_name: str

    async def ping(self) -> None: ...


async def preflight_providers(
    providers: Sequence[PingableProvider],
    alert: BriefingAlert | None = None,
    *,
    probe_timeout_seconds: float = _PROBE_TIMEOUT_SECONDS,
) -> None:
    """Log probe failures; only the final briefing outcome may page the operator.

    ``alert`` remains accepted for callers using the previous interface, but
    probes can recover or be covered by failover, so it is never invoked here.
    """
    del alert
    outcomes = await asyncio.gather(
        *(
            asyncio.wait_for(provider.ping(), timeout=probe_timeout_seconds)
            for provider in providers
        ),
        return_exceptions=True,
    )
    for provider, outcome in zip(providers, outcomes, strict=True):
        if isinstance(outcome, Exception):
            logger.warning(
                "briefing provider preflight failed: provider=%s error=%s",
                provider.provider_name,
                outcome.__class__.__name__,
            )


__all__ = ["preflight_providers"]
