"""Provider-neutral data structures for immutable podcast publication targets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from dailycast.db.models import (
    Episode,
    Publication,
    PublicationPlatform,
    PublicationTarget,
    PublicationTargetStatus,
)


@dataclass(frozen=True, slots=True)
class PublicAsset:
    """A verified immutable public media asset rooted under configured PUBLIC_DIR."""

    relative_path: str
    absolute_path: Path
    public_url: str
    sha256: str
    byte_size: int
    mime_type: str = "audio/mpeg"
    reused: bool = False


@dataclass(frozen=True, slots=True)
class RSSFeedItem:
    """An immutable Feed projection built before its Publication is marked published."""

    guid: str
    title: str
    description: str
    published_at: datetime
    duration_ms: int
    asset: PublicAsset


@dataclass(frozen=True, slots=True)
class FeedWriteResult:
    """Verification metadata for one atomically promoted RSS feed file."""

    feed_path: Path
    feed_version: str
    item_count: int


@dataclass(frozen=True, slots=True)
class PlatformPublishResult:
    """Stable remote identity returned by one platform-specific publisher."""

    remote_id: str | None = None
    remote_url: str | None = None


@dataclass(frozen=True, slots=True)
class DistributionResult:
    """Detached per-platform outcomes for one independent dispatch pass."""

    targets: tuple[PublicationTarget, ...]

    @property
    def published_platforms(self) -> tuple[PublicationPlatform, ...]:
        """Return successful platforms without treating other outcomes as a rollback."""
        return tuple(
            target.platform
            for target in self.targets
            if target.status is PublicationTargetStatus.PUBLISHED
        )

    @property
    def needs_attention_platforms(self) -> tuple[PublicationPlatform, ...]:
        """Return platforms that require a human login, captcha, or page review."""
        return tuple(
            target.platform
            for target in self.targets
            if target.status is PublicationTargetStatus.NEEDS_ATTENTION
        )


class PublisherError(RuntimeError):
    """One platform operation failed without invalidating the generated Episode."""


class PublisherNeedsAttentionError(PublisherError):
    """One platform requires a human action before only that target can resume."""


class Publisher(Protocol):
    """Independent distribution adapter implemented by every enabled platform."""

    platform_name: PublicationPlatform

    async def validate(self, episode: Episode) -> None:
        """Reject an Episode that cannot be delivered safely to this platform."""

    async def publish(self, episode: Episode) -> PlatformPublishResult:
        """Deliver one already generated Episode without changing generation artifacts."""

    async def check_status(
        self, episode: Episode, target: PublicationTarget
    ) -> PlatformPublishResult:
        """Inspect whether a previous side effect already completed remotely."""

    async def resume(self, episode: Episode, target: PublicationTarget) -> PlatformPublishResult:
        """Resume only this platform after a retryable or human-attention outcome."""


class RSSPublicationTarget(Protocol):
    """RSS-specific target capabilities used by the V1 immutable-media service."""

    target_key: str

    @property
    def feed_path(self) -> Path:
        """Return the public Feed destination rooted below configured PUBLIC_DIR."""

    def validate(self, episode: Episode, asset: PublicAsset) -> None:
        """Reject an Episode or asset that is unsafe for the RSS target."""

    def publish(self, items: tuple[RSSFeedItem, ...]) -> FeedWriteResult:
        """Atomically write a validated RSS representation."""

    def reconcile(self, publication: Publication, episode: Episode) -> bool:
        """Return whether files and Feed already prove publication completion."""

    def promote_asset(self, episode: Episode) -> PublicAsset:
        """Promote verified draft audio to a durable immutable public asset."""

    def feed_item(
        self, episode: Episode, publication: Publication, asset: PublicAsset
    ) -> RSSFeedItem:
        """Build one RSS item for the immutable public asset."""

    def asset_from_publication(self, publication: Publication) -> PublicAsset | None:
        """Reconstruct one safe immutable asset from durable Publication fields."""
