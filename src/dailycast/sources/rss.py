"""Standard RSS/Atom discovery using feedparser and the shared safe HTTP fetcher."""

from __future__ import annotations

import calendar
import json
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

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

    def __init__(
        self,
        fetcher: SafeHttpFetcher,
        *,
        rsshub_base_url: str | None = None,
        rsshub_access_key: str | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._rsshub_base_url = rsshub_base_url
        self._rsshub_access_key = rsshub_access_key

    async def collect(self, source: Source, window: CollectionWindow) -> CollectionResult:
        """Fetch, parse, window-filter, and map valid feed entries without database access."""
        title_keywords, filter_error = _title_filter(source)
        if filter_error is not None:
            return CollectionResult(source_id=source.id, error=filter_error)
        feed_url, route_error = _resolve_feed_url(
            source.entry_url, self._rsshub_base_url, self._rsshub_access_key
        )
        if route_error is not None:
            return CollectionResult(source_id=source.id, error=route_error)
        assert feed_url is not None
        try:
            response = await self._fetcher.fetch(
                feed_url,
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
            if title_keywords and not _matches_title(candidate.title, title_keywords):
                continue
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
        """Map one feedparser entry while making malformed entries an isolated warning.

        A feed entry without any date field stays explicitly undated here.
        Persistence assigns a one-time first-discovery fallback so repeated
        collection cannot refresh an old rolling-feed entry into the window.
        """
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
        published_at = RSSCollector._entry_datetime(entry)
        return (
            ArticleCandidate(
                source_id=source.id,
                external_id=external_id,
                url=urljoin(feed_url, raw_url.strip()),
                title=RSSCollector._plain_text(raw_title) or raw_title.strip(),
                summary=summary,
                content_text=content_text,
                published_at=published_at,
                published_at_inferred=published_at is None,
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


def _title_filter(source: Source) -> tuple[tuple[str, ...], SourceError | None]:
    """Read an optional source-level title allowlist without changing normal RSS feeds."""
    try:
        raw = json.loads(source.config_json)
        if not isinstance(raw, dict):
            raise ValueError("source config must be an object")
        keywords = raw.get("include_title_keywords")
        if keywords is None:
            return (), None
        if not isinstance(keywords, list) or not all(isinstance(value, str) for value in keywords):
            raise ValueError("include_title_keywords must be a list of strings")
        cleaned = tuple(value.strip() for value in keywords if value.strip())
        if not cleaned:
            raise ValueError("include_title_keywords must not be empty")
        return cleaned, None
    except (json.JSONDecodeError, TypeError, ValueError):
        return (), SourceError(
            code="INVALID_RSS_FILTER_CONFIG",
            summary="RSS title filter must be a non-empty list of strings",
            retryable=False,
        )


def _matches_title(title: str, keywords: tuple[str, ...]) -> bool:
    """Keep configured topical matches while preserving the original feed order."""
    normalized_title = title.casefold()
    return any(keyword.casefold() in normalized_title for keyword in keywords)


def _resolve_feed_url(
    entry_url: str,
    rsshub_base_url: str | None,
    rsshub_access_key: str | None = None,
) -> tuple[str | None, SourceError | None]:
    """Translate an RSSHub route only through a deployment-controlled HTTP(S) base URL."""
    route = urlsplit(entry_url)
    if route.scheme.lower() != "rsshub":
        return entry_url, None
    if rsshub_base_url is None:
        return None, SourceError(
            code="RSSHUB_BASE_URL_REQUIRED",
            summary="RSSHub source requires briefing.rsshub_base_url",
            retryable=False,
        )
    base = urlsplit(rsshub_base_url)
    if (
        base.scheme.lower() not in {"http", "https"}
        or base.hostname is None
        or base.username is not None
        or base.password is not None
        or route.username is not None
        or route.password is not None
        or route.hostname is None
    ):
        return None, SourceError(
            code="INVALID_RSSHUB_BASE_URL",
            summary="RSSHub base URL must be an absolute HTTP(S) URL without credentials",
            retryable=False,
        )
    try:
        _ = base.port
        if route.port is not None:
            return None, SourceError(
                code="INVALID_RSSHUB_ROUTE",
                summary="RSSHub route cannot include a port",
                retryable=False,
            )
    except ValueError:
        return None, SourceError(
            code="INVALID_RSSHUB_BASE_URL",
            summary="RSSHub base URL or route has an invalid port",
            retryable=False,
        )
    if not route.path.startswith("/"):
        return None, SourceError(
            code="INVALID_RSSHUB_ROUTE",
            summary="RSSHub route must include a path",
            retryable=False,
        )
    resolved_path = "/".join(
        part.strip("/") for part in (base.path, route.hostname, route.path) if part.strip("/")
    )
    if not resolved_path:
        return None, SourceError(
            code="INVALID_RSSHUB_ROUTE",
            summary="RSSHub route must include a path",
            retryable=False,
        )
    authority = base.netloc
    query = route.query
    if rsshub_access_key:
        # The instance-level RSSHub ACCESS_KEY travels through the environment,
        # never through the committed source seeds.
        pairs = parse_qsl(query, keep_blank_values=True)
        pairs.append(("key", rsshub_access_key))
        query = urlencode(pairs)
    return (
        urlunsplit(
            (
                base.scheme.lower(),
                authority,
                f"/{resolved_path}",
                query,
                "",
            )
        ),
        None,
    )


class _PlainTextParser(HTMLParser):
    """Minimal local HTML-to-text helper for untrusted feed title and summary fields."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        """Collect textual content while dropping all markup and attributes."""
        self.parts.append(data)
