"""Conservative discovery from official HTML announcement-list pages."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dailycast.db.models import Source
from dailycast.sources.contracts import (
    ArticleCandidate,
    CollectionResult,
    CollectionWindow,
    SourceError,
)
from dailycast.sources.extraction import FetchPolicy, SafeHttpFetcher, SourceFetchError

_DATE_PATTERN = re.compile(r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b")
_WHITESPACE_PATTERN = re.compile(r"\s+")
_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


@dataclass(frozen=True, slots=True)
class _HTMLListOptions:
    """Per-source list discovery boundaries persisted in the Source config JSON."""

    article_url_path_prefixes: tuple[str, ...]
    timezone: ZoneInfo
    allowed_host: str
    include_title_keywords: tuple[str, ...] = ()


@dataclass(slots=True)
class _Node:
    """Small, attribute-free tree node for extracting safe text from announcement pages."""

    tag: str
    attributes: dict[str, str]
    parent: _Node | None = None
    children: list[_Node] = field(default_factory=list)
    text_parts: list[str] = field(default_factory=list)


class HTMLListCollector:
    """Discover matching official notices from a bounded, same-host HTML listing."""

    def __init__(self, fetcher: SafeHttpFetcher) -> None:
        self._fetcher = fetcher

    async def collect(self, source: Source, window: CollectionWindow) -> CollectionResult:
        """Fetch a list page and map qualifying announcement anchors into candidates."""
        options, option_error = _parse_options(source)
        if option_error is not None:
            return CollectionResult(source_id=source.id, error=option_error)
        assert options is not None
        try:
            response = await self._fetcher.fetch(
                source.entry_url,
                FetchPolicy(timeout_seconds=float(source.request_timeout_seconds or 20)),
            )
        except SourceFetchError as error:
            return CollectionResult(source_id=source.id, error=error.error)

        content_type = response.headers.get("content-type", "").lower().split(";", maxsplit=1)[0]
        if content_type not in {"text/html", "application/xhtml+xml"}:
            return CollectionResult(
                source_id=source.id,
                error=SourceError(
                    code="UNSUPPORTED_CONTENT_TYPE",
                    summary="announcement list response is not HTML",
                    retryable=False,
                ),
            )

        parser = _ListPageParser()
        try:
            parser.feed(response.content.decode("utf-8", errors="replace"))
            parser.close()
        except Exception as error:
            return CollectionResult(
                source_id=source.id,
                error=SourceError(
                    code="HTML_LIST_PARSE_ERROR",
                    summary=f"announcement list could not be parsed: {error.__class__.__name__}",
                    retryable=False,
                ),
            )

        candidates: list[ArticleCandidate] = []
        errors: list[SourceError] = []
        seen_urls: set[str] = set()
        for anchor in _iter_anchors(parser.root):
            if len(candidates) >= (source.max_items_per_run or 50):
                break
            title = _text_content(anchor)
            if (
                options.include_title_keywords
                and not _matches_keywords(title, options.include_title_keywords)
            ):
                continue
            candidate, entry_error = _to_candidate(source, options, anchor, response.final_url)
            if entry_error is not None:
                errors.append(entry_error)
                continue
            if candidate is None:
                continue
            if candidate.url in seen_urls or not window.includes(candidate.published_at):
                continue
            seen_urls.add(candidate.url)
            candidates.append(candidate)
        return CollectionResult(
            source_id=source.id,
            candidates=tuple(candidates),
            errors=tuple(errors),
        )


class _ListPageParser(HTMLParser):
    """Parse the limited HTML structure needed to retain anchor text and nearby dates."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node(tag="document", attributes={})
        self._stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.lower()
        node = _Node(
            tag=normalized_tag,
            attributes={key.lower(): value or "" for key, value in attrs},
            parent=self._stack[-1],
        )
        self._stack[-1].children.append(node)
        if normalized_tag not in _VOID_TAGS:
            self._stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.lower()
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == normalized_tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self._stack[-1].text_parts.append(data)


def _parse_options(source: Source) -> tuple[_HTMLListOptions | None, SourceError | None]:
    """Validate the small fixed option set without evaluating arbitrary source configuration."""
    try:
        raw = json.loads(source.config_json)
        keywords = raw.get("include_title_keywords", [])
        path_prefixes = raw.get("article_url_path_prefixes", [])
        timezone_name = raw.get("timezone", "UTC")
        if not isinstance(keywords, list) or not all(isinstance(value, str) for value in keywords):
            raise ValueError("include_title_keywords must be a list of strings")
        cleaned_keywords = tuple(value.strip() for value in keywords if value.strip())
        if not isinstance(path_prefixes, list) or not all(
            isinstance(value, str) for value in path_prefixes
        ):
            raise ValueError("article_url_path_prefixes must be a list of strings")
        cleaned_path_prefixes = tuple(value.strip() for value in path_prefixes if value.strip())
        if any(not value.startswith("/") for value in cleaned_path_prefixes):
            raise ValueError("article_url_path_prefixes must start with '/'")
        if not isinstance(timezone_name, str):
            raise ValueError("timezone must be a string")
        entry_host = urlsplit(source.entry_url).hostname
        if entry_host is None:
            raise ValueError("entry_url must contain a host")
        return (
            _HTMLListOptions(
                include_title_keywords=cleaned_keywords,
                article_url_path_prefixes=cleaned_path_prefixes,
                timezone=ZoneInfo(timezone_name),
                allowed_host=entry_host.lower(),
            ),
            None,
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, ZoneInfoNotFoundError):
        return None, SourceError(
            code="INVALID_HTML_LIST_CONFIG",
            summary="html_list source requires an optional title filter and a valid timezone",
            retryable=False,
        )


def _iter_anchors(node: _Node) -> Iterator[_Node]:
    """Yield anchors in document order without exposing untrusted attributes to callers."""
    for child in node.children:
        if child.tag == "a":
            yield child
        yield from _iter_anchors(child)


def _to_candidate(
    source: Source, options: _HTMLListOptions, anchor: _Node, list_url: str
) -> tuple[ArticleCandidate | None, SourceError | None]:
    """Map one dated, matched same-host anchor from an announcement list.

    Unlike RSS, an HTML list usually also contains navigation and topic links.
    Requiring a nearby date keeps those links, and an old page re-observed on a
    later run, from becoming apparently new articles.
    """
    href = anchor.attributes.get("href", "").strip()
    title = _text_content(anchor)
    if not href:
        return None, SourceError(
            code="INVALID_HTML_LIST_ENTRY",
            summary="matching announcement has no article URL",
            retryable=False,
        )
    article_url = urljoin(list_url, href)
    parsed_url = urlsplit(article_url)
    if parsed_url.scheme.lower() not in {"http", "https"} or parsed_url.hostname is None:
        return None, SourceError(
            code="INVALID_HTML_LIST_ENTRY",
            summary="matching announcement has an invalid article URL",
            retryable=False,
        )
    if parsed_url.hostname.lower() != options.allowed_host:
        return None, None
    if options.article_url_path_prefixes and not any(
        parsed_url.path.startswith(prefix) for prefix in options.article_url_path_prefixes
    ):
        return None, None
    if not title:
        return None, SourceError(
            code="INVALID_HTML_LIST_ENTRY",
            summary="matching announcement has no title",
            retryable=False,
        )
    published_at = _nearby_date(anchor, options.timezone)
    if published_at is None:
        return None, SourceError(
            code="MISSING_PUBLICATION_DATE",
            summary="matching announcement has no nearby publication date",
            retryable=False,
        )
    return (
        ArticleCandidate(
            source_id=source.id,
            external_id=article_url,
            url=article_url,
            title=title,
            published_at=published_at,
            language=source.language,
            metadata={"list_url": list_url},
        ),
        None,
    )


def _matches_keywords(title: str, keywords: tuple[str, ...]) -> bool:
    """Keep recruitment notices while leaving navigation and unrelated policy pages out."""
    normalized_title = title.casefold()
    return bool(normalized_title) and any(
        keyword.casefold() in normalized_title for keyword in keywords
    )


def _nearby_date(anchor: _Node, timezone: ZoneInfo) -> datetime | None:
    """Read one ISO-like publication date from the closest list-item context, when available."""
    context = anchor
    while context.parent is not None and context.tag not in {"li", "article", "tr"}:
        context = context.parent
    matched = _DATE_PATTERN.search(_text_content(context))
    if matched is None:
        return None
    try:
        return datetime(
            int(matched.group(1)),
            int(matched.group(2)),
            int(matched.group(3)),
            tzinfo=timezone,
        ).astimezone(UTC)
    except ValueError:
        return None


def _text_content(node: _Node) -> str:
    """Return collapsed descendant text without retaining markup or attributes."""
    parts = list(node.text_parts)
    for child in node.children:
        parts.append(_text_content(child))
    return _WHITESPACE_PATTERN.sub(" ", " ".join(parts)).strip()
