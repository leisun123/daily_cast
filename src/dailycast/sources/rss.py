"""Standard RSS/Atom discovery using feedparser and the shared safe HTTP fetcher."""

from __future__ import annotations

import calendar
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

import feedparser

from dailycast.db.models import Source
from dailycast.sources.contracts import (
    ArticleCandidate,
    CollectionResult,
    CollectionWindow,
    SourceError,
)
from dailycast.sources.extraction import FetchPolicy, SafeHttpFetcher, SourceFetchError


class RSSCollector:
    """Discover bounded article candidates from one configured RSS or Atom feed."""

    def __init__(self, fetcher: SafeHttpFetcher) -> None:
        self._fetcher = fetcher

    async def collect(self, source: Source, window: CollectionWindow) -> CollectionResult:
        """Fetch, parse, window-filter, and map valid feed entries without database access."""
        try:
            response = await self._fetcher.fetch(
                source.entry_url,
                FetchPolicy(timeout_seconds=float(source.request_timeout_seconds or 20)),
            )
        except SourceFetchError as error:
            return CollectionResult(source_id=source.id, error=error.error)

        parsed = feedparser.parse(response.content)
        entries = list(parsed.get("entries", []))
        if not entries and getattr(parsed, "bozo", False):
            return CollectionResult(
                source_id=source.id,
                error=SourceError(
                    code="RSS_PARSE_ERROR",
                    summary="source response could not be parsed as an RSS or Atom feed",
                    retryable=False,
                ),
            )

        candidates: list[ArticleCandidate] = []
        errors: list[SourceError] = []
        for entry in entries:
            if len(candidates) >= (source.max_items_per_run or 50):
                break
            candidate, entry_error = self._to_candidate(source, entry, response.final_url)
            if entry_error is not None:
                errors.append(entry_error)
                continue
            assert candidate is not None
            if window.includes(candidate.published_at):
                candidates.append(candidate)
        return CollectionResult(
            source_id=source.id,
            candidates=tuple(candidates),
            errors=tuple(errors),
        )

    @staticmethod
    def _to_candidate(
        source: Source, entry: Any, feed_url: str
    ) -> tuple[ArticleCandidate | None, SourceError | None]:
        """Map one feedparser entry while making malformed entries an isolated warning."""
        raw_url = entry.get("link")
        raw_title = entry.get("title")
        if not isinstance(raw_url, str) or not raw_url.strip():
            return None, SourceError(
                code="INVALID_RSS_ENTRY",
                summary="feed entry has no article URL",
                retryable=False,
            )
        if not isinstance(raw_title, str) or not raw_title.strip():
            return None, SourceError(
                code="INVALID_RSS_ENTRY",
                summary="feed entry has no title",
                retryable=False,
            )
        summary = RSSCollector._plain_text(entry.get("summary") or entry.get("description"))
        content_text = RSSCollector._entry_content(entry)
        raw_external_id = entry.get("id") or entry.get("guid")
        external_id = str(raw_external_id) if raw_external_id is not None else None
        return (
            ArticleCandidate(
                source_id=source.id,
                external_id=external_id,
                url=urljoin(feed_url, raw_url.strip()),
                title=RSSCollector._plain_text(raw_title) or raw_title.strip(),
                summary=summary,
                content_text=content_text,
                published_at=RSSCollector._entry_datetime(entry),
                language=source.language,
                metadata={"feed_url": feed_url},
            ),
            None,
        )

    @staticmethod
    def _entry_content(entry: Any) -> str | None:
        """Return the first supplied RSS full-content field as plain text, if present."""
        raw_content = entry.get("content")
        if not isinstance(raw_content, list) or not raw_content:
            return None
        first = raw_content[0]
        if not isinstance(first, dict):
            return None
        value = first.get("value")
        return RSSCollector._plain_text(value)

    @staticmethod
    def _entry_datetime(entry: Any) -> datetime | None:
        """Translate feedparser UTC struct times without using the host timezone."""
        for key in ("published_parsed", "updated_parsed"):
            parsed_time = entry.get(key)
            if parsed_time is not None:
                return datetime.fromtimestamp(calendar.timegm(parsed_time), tz=UTC)
        return None

    @staticmethod
    def _plain_text(value: object) -> str | None:
        """Drop feed HTML markup without preserving it as a renderable article field."""
        if not isinstance(value, str):
            return None
        parser = _PlainTextParser()
        parser.feed(value)
        text = " ".join(" ".join(parser.parts).split())
        text = re.sub(r"\s+([,.!?;:])", r"\1", text)
        return text or None


class _PlainTextParser(HTMLParser):
    """Minimal local HTML-to-text helper for untrusted feed title and summary fields."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        """Collect textual content while dropping all markup and attributes."""
        self.parts.append(data)
