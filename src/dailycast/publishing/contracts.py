"""Provider-neutral data structures for immutable podcast publication targets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from dailycast.db.models import Episode, Publication


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


class Publisher(Protocol):
    """Isolate publication lifecycle code from the local RSS filesystem implementation."""

    target_key: str

    def validate(self, episode: Episode, asset: PublicAsset) -> None:
        """Reject an Episode or asset that is unsafe to publish to this target."""

    def publish(self, items: tuple[RSSFeedItem, ...]) -> FeedWriteResult:
        """Atomically write a validated target representation for the supplied Feed items."""

    def reconcile(self, publication: Publication, episode: Episode) -> bool:
        """Return whether durable target state already proves Publication completion."""


class RSSPublicationTarget(Publisher, Protocol):
    """RSS-specific target capabilities used by the V1 immutable-media service."""

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
