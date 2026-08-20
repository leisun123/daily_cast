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
    """One adapter's externally verified publication outcome."""

    status: PublicationTargetStatus
    remote_id: str | None = None
    remote_url: str | None = None
    last_error: str | None = None
    asset: PublicAsset | None = None
    rss_publication: Publication | None = None


@dataclass(frozen=True, slots=True)
class DistributionResult:
    """Aggregate target states without treating one platform failure as episode failure."""

    rss_publication: Publication | None
    target_statuses: dict[str, str]
    warning_count: int


class Publisher(Protocol):
    """One independently recoverable external distribution destination."""

    @property
    def platform_name(self) -> PublicationPlatform:
        """Return the immutable platform identity owned by this adapter."""

    async def validate(
        self, episode: Episode, target: PublicationTarget, asset: PublicAsset | None
    ) -> None:
        """Reject a target operation before it causes a platform side effect."""

    async def publish(
        self, episode: Episode, target: PublicationTarget, asset: PublicAsset | None
    ) -> PlatformPublishResult:
        """Publish one target and return only its independently durable outcome."""

    async def check_status(
        self, episode: Episode, target: PublicationTarget, asset: PublicAsset | None
    ) -> PlatformPublishResult:
        """Reconcile target state after a restart or an ambiguous platform response."""

    async def resume(
        self, episode: Episode, target: PublicationTarget, asset: PublicAsset | None
    ) -> PlatformPublishResult:
        """Continue the same target row after an explicit human action has resolved."""


class RSSPublicationTarget(Protocol):
    """RSS-specific target capabilities used by the V1 immutable-media service."""

    target_key: str

    def validate(self, episode: Episode, asset: PublicAsset) -> None:
        """Validate an immutable asset before it can enter the RSS Feed."""

    def publish(self, items: tuple[RSSFeedItem, ...]) -> FeedWriteResult:
        """Atomically write and verify the complete RSS Feed projection."""

    def reconcile(self, publication: Publication, episode: Episode) -> bool:
        """Verify a durable publication has its exact immutable Feed item."""

    @property
    def feed_path(self) -> Path:
        """Return the public Feed destination rooted below configured PUBLIC_DIR."""

    def promote_asset(self, episode: Episode) -> PublicAsset:
        """Promote verified draft audio to a durable immutable public asset."""

    def feed_item(
        self, episode: Episode, publication: Publication, asset: PublicAsset
    ) -> RSSFeedItem:
        """Build one RSS item for the immutable public asset."""

    def asset_from_publication(self, publication: Publication) -> PublicAsset | None:
        """Reconstruct one safe immutable asset from durable Publication fields."""
