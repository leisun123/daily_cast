"""RSS-claim adapter for Xiaoyuzhou without browser automation or direct upload."""

from __future__ import annotations

from urllib.parse import urlparse

from dailycast.db.models import Episode, PublicationPlatform, PublicationTarget
from dailycast.publishing.contracts import (
    PlatformPublishResult,
    PublisherNeedsAttentionError,
)


class XiaoyuzhouPublisher:
    """Represent the external RSS claim while keeping DailyCast as the hosting source."""

    platform_name = PublicationPlatform.XIAOYUZHOU

    def __init__(self, *, program_url: str | None) -> None:
        if program_url is not None:
            parsed = urlparse(program_url)
            if (
                parsed.scheme != "https"
                or parsed.hostname != "www.xiaoyuzhoufm.com"
                or "/podcast/" not in parsed.path
            ):
                raise ValueError("Xiaoyuzhou program_url must be an official HTTPS podcast URL")
        self._program_url = program_url

    async def validate(self, episode: Episode) -> None:
        """Require only a persisted generated Episode; Xiaoyuzhou consumes its RSS."""
        if episode.id <= 0:
            raise ValueError("Xiaoyuzhou distribution requires a persisted Episode")

    async def publish(self, episode: Episode) -> PlatformPublishResult:
        """Record an already claimed RSS program or request the one manual import action."""
        await self.validate(episode)
        return self._claimed_result()

    async def check_status(
        self, episode: Episode, target: PublicationTarget
    ) -> PlatformPublishResult:
        """Return the configured claim identity without making a platform request."""
        del target
        await self.validate(episode)
        return self._claimed_result()

    async def resume(self, episode: Episode, target: PublicationTarget) -> PlatformPublishResult:
        """Re-evaluate only Xiaoyuzhou after the operator configures its claimed URL."""
        del target
        await self.validate(episode)
        return self._claimed_result()

    def _claimed_result(self) -> PlatformPublishResult:
        if self._program_url is None:
            raise PublisherNeedsAttentionError("XIAOYUZHOU_RSS_IMPORT_REQUIRED")
        remote_id = self._program_url.rstrip("/").rsplit("/", 1)[-1]
        return PlatformPublishResult(remote_id=remote_id, remote_url=self._program_url)
