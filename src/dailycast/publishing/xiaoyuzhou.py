"""Preparation-only Xiaoyuzhou target that keeps future RSS distribution decoupled."""

from __future__ import annotations

from dailycast.db.models import (
    Episode,
    PublicationPlatform,
    PublicationTarget,
    PublicationTargetStatus,
)
from dailycast.publishing.contracts import PlatformPublishResult, PublicAsset


class XiaoyuzhouPublisher:
    """Disabled adapter placeholder with a complete contract and no browser/API side effects."""

    platform_name = PublicationPlatform.XIAOYUZHOU

    async def validate(
        self, episode: Episode, target: PublicationTarget, asset: PublicAsset | None
    ) -> None:
        """Keep the adapter callable without treating RSS consumption as an implemented upload."""
        del episode, target, asset

    async def publish(
        self, episode: Episode, target: PublicationTarget, asset: PublicAsset | None
    ) -> PlatformPublishResult:
        """Report an explicit human-action state until a supported route is implemented."""
        del episode, target, asset
        return PlatformPublishResult(
            status=PublicationTargetStatus.NEEDS_ATTENTION,
            last_error="XIAOYUZHOU_NOT_IMPLEMENTED: configure RSS distribution outside DailyCast",
        )

    async def check_status(
        self, episode: Episode, target: PublicationTarget, asset: PublicAsset | None
    ) -> PlatformPublishResult:
        """Do not infer remote status before DailyCast owns a Xiaoyuzhou publish action."""
        del episode, target, asset
        return PlatformPublishResult(status=PublicationTargetStatus.PENDING)

    async def resume(
        self, episode: Episode, target: PublicationTarget, asset: PublicAsset | None
    ) -> PlatformPublishResult:
        """Keep resume safe and explicit instead of pretending an RSS consumer was published."""
        return await self.publish(episode, target, asset)
