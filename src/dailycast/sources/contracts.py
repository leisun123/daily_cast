"""Typed data contracts for source discovery and article extraction."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from dailycast.db.models import Source


@dataclass(frozen=True, slots=True)
class SourceError:
    """A bounded, structured source or article error safe to persist in audit fields."""

    code: str
    summary: str
    retryable: bool


@dataclass(frozen=True, slots=True)
class ArticleCandidate:
    """One article discovered from a configured source before persistence."""

    source_id: str
    url: str
    title: str
    external_id: str | None = None
    summary: str | None = None
    content_text: str | None = None
    published_at: datetime | None = None
    published_at_inferred: bool = False
    language: str | None = None
    fetched_at: datetime | None = None
    http_status: int | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CollectionWindow:
    """Half-open time window applied to entries that provide a publication timestamp.

    A daily briefing spans ``[start, end)``.  Treating ``end`` as exclusive
    prevents an article published exactly at the next Shanghai midnight from
    leaking into the previous day's briefing.
    """

    start: datetime
    end: datetime

    def includes(self, published_at: datetime | None) -> bool:
        """Retain undated entries while excluding dated entries outside the requested period."""
        if published_at is None:
            return True
        return self._as_utc(self.start) <= self._as_utc(published_at) < self._as_utc(self.end)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        """Normalize SQLite-like naive values defensively before window comparisons."""
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class CollectionResult:
    """Candidates and source-scoped warnings produced by one collection attempt."""

    source_id: str
    candidates: tuple[ArticleCandidate, ...] = ()
    errors: tuple[SourceError, ...] = ()
    error: SourceError | None = None


@dataclass(frozen=True, slots=True)
class ExtractedArticle:
    """A successful text extraction or its structured article-level failure."""

    requested_url: str
    final_url: str | None
    content_text: str | None
    http_status: int | None
    fetched_at: datetime | None
    error: SourceError | None = None
    published_at: datetime | None = None


class SourceCollector(Protocol):
    """Discover candidates from one configured Source without accessing SQLAlchemy."""

    async def collect(self, source: Source, window: CollectionWindow) -> CollectionResult:
        """Collect at most the source's configured candidate limit for the time window."""
