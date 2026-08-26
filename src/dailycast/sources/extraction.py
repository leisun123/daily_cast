"""Bounded HTTP fetching and trafilatura extraction with SSRF protections."""

from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import re
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from html.parser import HTMLParser
from typing import NoReturn, Protocol
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import httpx
import trafilatura

from dailycast.sources.contracts import ExtractedArticle, SourceError

_CHALLENGE_MARKERS = (
    "just a moment",
    "security check",
    "captcha",
    "access denied",
    "验证码",
    "访问验证",
    "人机验证",
)
_VISIBLE_PUBLICATION_DATE = re.compile(
    r"(?:发布时间|发布日期|发稿时间|发表时间)\s*[:：]?\s*"
    r"(20\d{2}(?:[-/.]\d{1,2}[-/.]\d{1,2}|年\d{1,2}月\d{1,2}日)"
    r"(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?(?:Z|[+-]\d{2}:?\d{2})?)"
)
_VISIBLE_SOURCE_HEADER_DATE = re.compile(
    r"(20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}"
    r"(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?)\s*(?:\||丨|│)?\s*来源\s*[:：]?"
)
_C114_ARTICLE_HEADER_DATE = re.compile(
    r'<div\b[^>]*\bclass\s*=\s*["\'][^"\']*\barticle_top\b[^"\']*["\'][^>]*>'
    r'.*?<div\b[^>]*\bclass\s*=\s*["\'][^"\']*\btime\b[^"\']*["\'][^>]*>\s*'
    r"(20\d{2}[/.-]\d{1,2}[/.-]\d{1,2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?)\s*</div>"
    r'.*?<h1\b[^>]*\bclass\s*=\s*["\'][^"\']*\barticle_title\b[^"\']*["\']',
    re.IGNORECASE | re.DOTALL,
)
_CHINESE_DATE = re.compile(
    r"^(?P<year>20\d{2})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日"
    r"(?:[ T](?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?)?$"
)
_NUMERIC_DATE = re.compile(
    r"^(?P<year>20\d{2})[-/.](?P<month>\d{1,2})[-/.](?P<day>\d{1,2})"
    r"(?:[ T](?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?)?"
    r"(?P<timezone>Z|[+-]\d{2}:?\d{2})?$"
)
_DECLARED_HTML_CHARSET = re.compile(rb"\bcharset\s*=\s*[\"']?([a-z0-9._-]+)", re.IGNORECASE)
_HEADER_CHARSET = re.compile(r"\bcharset\s*=\s*[\"']?([a-z0-9._-]+)", re.IGNORECASE)
_C114_TIMEZONE = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True, slots=True)
class FetchPolicy:
    """Bounded network-fetch settings for one source or article request."""

    timeout_seconds: float
    max_response_bytes: int = 5 * 1024 * 1024
    max_redirects: int = 3


@dataclass(frozen=True, slots=True)
class FetchedResponse:
    """A safely downloaded response after every redirect target has been validated."""

    final_url: str
    status_code: int
    headers: Mapping[str, str]
    content: bytes
    fetched_at: datetime


class UrlSafetyValidator(Protocol):
    """Validate a requested URL before every outbound HTTP request."""

    def validate(self, url: str) -> None:
        """Raise SourceFetchError when the target is outside the network safety boundary."""


class SourceFetchError(RuntimeError):
    """An HTTP fetch failure represented by a stable code and a short safe summary."""

    def __init__(self, error: SourceError) -> None:
        super().__init__(error.summary)
        self.error = error


class StrictUrlSafetyValidator:
    """Reject local, private, link-local, and non-HTTP(S) targets before connection."""

    def validate(self, url: str) -> None:
        """Resolve the hostname and reject every non-global resolved address."""
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"}:
            self._raise(
                "UNSUPPORTED_URL_SCHEME", "only HTTP and HTTPS source URLs are allowed", False
            )
        if parsed.username is not None or parsed.password is not None:
            self._raise("UNSAFE_URL", "source URL credentials are not allowed", False)
        hostname = parsed.hostname
        if hostname is None:
            self._raise("INVALID_URL", "source URL must include a hostname", False)
        try:
            port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        except ValueError:
            self._raise("INVALID_URL", "source URL has an invalid port", False)
        host = hostname.rstrip(".")
        try:
            addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as error:
            message = f"could not resolve source hostname: {error.__class__.__name__}"
            self._raise("DNS_RESOLUTION_FAILED", message, True)
        resolved_ips = {address[4][0] for address in addresses}
        if not resolved_ips:
            self._raise("DNS_RESOLUTION_FAILED", "source hostname resolved to no addresses", True)
        for raw_ip in resolved_ips:
            ip = ipaddress.ip_address(raw_ip)
            if not ip.is_global:
                self._raise("SSRF_BLOCKED", "source URL resolved to a non-public address", False)

    @staticmethod
    def _raise(code: str, summary: str, retryable: bool) -> NoReturn:
        raise SourceFetchError(SourceError(code=code, summary=summary, retryable=retryable))


class SafeHttpFetcher:
    """Fetch bounded HTTP responses while validating initial and redirect targets."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        url_validator: UrlSafetyValidator | None = None,
    ) -> None:
        self._client = client
        self._url_validator = url_validator or StrictUrlSafetyValidator()

    async def fetch(self, url: str, policy: FetchPolicy) -> FetchedResponse:
        """Download one response, enforcing size, status, redirect, and SSRF boundaries."""
        if self._client is not None:
            return await self._fetch_with_client(self._client, url, policy)
        timeout = httpx.Timeout(policy.timeout_seconds)
        async with httpx.AsyncClient(
            follow_redirects=False,
            headers={"User-Agent": "DailyCast/0.1 (+https://github.com/)"},
            timeout=timeout,
        ) as client:
            return await self._fetch_with_client(client, url, policy)

    async def _fetch_with_client(
        self, client: httpx.AsyncClient, url: str, policy: FetchPolicy
    ) -> FetchedResponse:
        """Perform manual redirect following so every URL is independently safety-checked."""
        current_url = url
        for redirect_count in range(policy.max_redirects + 1):
            self._url_validator.validate(current_url)
            response: httpx.Response | None = None
            try:
                request = client.build_request("GET", current_url, timeout=policy.timeout_seconds)
                response = await client.send(request, stream=True, follow_redirects=False)
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if location is None:
                        self._raise(
                            "REDIRECT_INVALID", "redirect response has no Location header", False
                        )
                    if redirect_count >= policy.max_redirects:
                        self._raise("REDIRECT_LIMIT", "source URL exceeded redirect limit", False)
                    current_url = urljoin(current_url, location)
                    continue
                if not 200 <= response.status_code < 300:
                    retryable = response.status_code == 429 or response.status_code >= 500
                    self._raise(
                        "HTTP_STATUS",
                        f"source returned HTTP {response.status_code}",
                        retryable,
                    )
                content_length = response.headers.get("content-length")
                if content_length is not None and int(content_length) > policy.max_response_bytes:
                    self._raise(
                        "RESPONSE_TOO_LARGE",
                        "source response exceeded configured size limit",
                        False,
                    )
                chunks: list[bytes] = []
                total_size = 0
                async for chunk in response.aiter_bytes():
                    total_size += len(chunk)
                    if total_size > policy.max_response_bytes:
                        self._raise(
                            "RESPONSE_TOO_LARGE",
                            "source response exceeded configured size limit",
                            False,
                        )
                    chunks.append(chunk)
                return FetchedResponse(
                    final_url=str(response.url),
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    content=b"".join(chunks),
                    fetched_at=datetime.now(UTC),
                )
            except SourceFetchError:
                raise
            except httpx.TimeoutException as error:
                self._raise(
                    "TIMEOUT", f"source request timed out: {error.__class__.__name__}", True
                )
            except httpx.RequestError as error:
                self._raise(
                    "NETWORK_ERROR", f"source request failed: {error.__class__.__name__}", True
                )
            except (TypeError, ValueError) as error:
                self._raise(
                    "INVALID_RESPONSE",
                    f"source response was invalid: {error.__class__.__name__}",
                    False,
                )
            finally:
                if response is not None:
                    await response.aclose()
        self._raise("REDIRECT_LIMIT", "source URL exceeded redirect limit", False)

    @staticmethod
    def _raise(code: str, summary: str, retryable: bool) -> NoReturn:
        raise SourceFetchError(SourceError(code=code, summary=summary, retryable=retryable))


class ContentExtractor:
    """Extract clean article text from a safely bounded HTML response."""

    def __init__(self, fetcher: SafeHttpFetcher) -> None:
        self._fetcher = fetcher

    async def extract(self, url: str, policy: FetchPolicy) -> ExtractedArticle:
        """Return text or a structured article-level error without raising to the pipeline."""
        try:
            response = await self._fetcher.fetch(url, policy)
        except SourceFetchError as error:
            return ExtractedArticle(
                requested_url=url,
                final_url=None,
                content_text=None,
                http_status=None,
                fetched_at=None,
                error=error.error,
            )
        raw_content_type = response.headers.get("content-type", "")
        content_type = raw_content_type.lower().split(";", maxsplit=1)[0]
        if content_type not in {"text/html", "application/xhtml+xml"}:
            return ExtractedArticle(
                requested_url=url,
                final_url=response.final_url,
                content_text=None,
                http_status=response.status_code,
                fetched_at=response.fetched_at,
                error=SourceError(
                    code="UNSUPPORTED_CONTENT_TYPE",
                    summary="article response is not HTML",
                    retryable=False,
                ),
            )
        html_text = _decode_html(response.content, raw_content_type)
        if _looks_like_access_challenge(html_text):
            return ExtractedArticle(
                requested_url=url,
                final_url=response.final_url,
                content_text=None,
                http_status=response.status_code,
                fetched_at=response.fetched_at,
                error=SourceError(
                    code="ACCESS_CHALLENGE",
                    summary="article response is an access verification challenge",
                    retryable=False,
                ),
            )
        try:
            content_text = await asyncio.to_thread(
                trafilatura.extract,
                html_text,
                url=response.final_url,
                output_format="txt",
                include_comments=False,
                include_tables=False,
            )
        except Exception as error:
            return ExtractedArticle(
                requested_url=url,
                final_url=response.final_url,
                content_text=None,
                http_status=response.status_code,
                fetched_at=response.fetched_at,
                error=SourceError(
                    code="EXTRACTION_PARSE_ERROR",
                    summary=f"article text extraction failed: {error.__class__.__name__}",
                    retryable=False,
                ),
            )
        if content_text is None or not content_text.strip():
            return ExtractedArticle(
                requested_url=url,
                final_url=response.final_url,
                content_text=None,
                http_status=response.status_code,
                fetched_at=response.fetched_at,
                error=SourceError(
                    code="EMPTY_CONTENT",
                    summary="article text extraction returned no usable content",
                    retryable=False,
                ),
            )
        return ExtractedArticle(
            requested_url=url,
            final_url=response.final_url,
            content_text=content_text,
            http_status=response.status_code,
            fetched_at=response.fetched_at,
            published_at=_published_at_from_html(html_text, response.final_url),
        )


def _decode_html(content: bytes, content_type: str) -> str:
    """Decode HTML using its HTTP or in-document charset before a safe UTF-8 fallback."""
    header_match = _HEADER_CHARSET.search(content_type)
    meta_match = _DECLARED_HTML_CHARSET.search(content[:4096])
    candidates = [
        header_match.group(1) if header_match is not None else None,
        meta_match.group(1).decode("ascii") if meta_match is not None else None,
        "utf-8",
    ]
    attempted: set[str] = set()
    for charset in candidates:
        if charset is None:
            continue
        normalized = charset.casefold()
        if normalized in attempted:
            continue
        attempted.add(normalized)
        try:
            return content.decode(charset)
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode("utf-8", errors="replace")


class _PublicationDateParser(HTMLParser):
    """Read only explicit publication-time signals from the fetched HTML document."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._meta_dates: list[str] = []
        self._json_ld_dates: list[str] = []
        self._time_dates: list[str] = []
        self._json_ld_parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        if tag.lower() == "meta":
            property_name = values.get("property", "").lower()
            name = values.get("name", "").lower()
            if property_name == "article:published_time" or name in {
                "article:published_time",
                "datepublished",
                "publishdate",
                "pubdate",
                "publish_date",
                "publication_date",
            }:
                self._meta_dates.append(values.get("content", ""))
        elif tag.lower() == "time":
            self._time_dates.append(values.get("datetime", ""))
        elif tag.lower() == "script" and values.get("type", "").lower() == "application/ld+json":
            self._json_ld_parts = []

    def handle_data(self, data: str) -> None:
        if self._json_ld_parts is not None:
            self._json_ld_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "script" or self._json_ld_parts is None:
            return
        raw_json = "".join(self._json_ld_parts)
        self._json_ld_parts = None
        try:
            payload = json.loads(raw_json)
        except json.JSONDecodeError:
            return
        self._json_ld_dates.extend(_json_ld_published_dates(payload))

    def candidate_dates(self) -> tuple[str, ...]:
        """Keep the explicit metadata precedence stable across publishers."""
        return tuple(self._meta_dates + self._json_ld_dates + self._time_dates)


def _published_at_from_html(html_text: str, page_url: str) -> datetime | None:
    """Return the first valid explicit page publication time in UTC, if present."""
    parser = _PublicationDateParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:
        return None
    c114_header_dates = _c114_article_header_dates(html_text, page_url)
    visible_dates = _visible_publication_dates(html_text)
    visible_source_header_dates = _visible_source_header_publication_dates(html_text)
    default_timezone = _default_publication_timezone(page_url)
    for raw_value in parser.candidate_dates():
        parsed = _parse_publication_datetime(raw_value, default_timezone=default_timezone)
        if parsed is not None:
            return parsed
    for raw_value in c114_header_dates:
        parsed = _parse_publication_datetime(raw_value, default_timezone=_C114_TIMEZONE)
        if parsed is not None:
            return parsed
    for raw_value in visible_dates:
        parsed = _parse_publication_datetime(raw_value, default_timezone=default_timezone)
        if parsed is not None:
            return parsed
    for raw_value in visible_source_header_dates:
        parsed = _parse_publication_datetime(raw_value, default_timezone=_C114_TIMEZONE)
        if parsed is not None:
            return parsed
    return None


def _default_publication_timezone(page_url: str) -> tzinfo:
    """Interpret naive timestamps on mainland Chinese domains as Asia/Shanghai."""
    hostname = urlsplit(page_url).hostname
    if hostname is not None and hostname.casefold().rstrip(".").endswith(".cn"):
        return _C114_TIMEZONE
    return UTC


def _c114_article_header_dates(html_text: str, page_url: str) -> tuple[str, ...]:
    """Read C114's primary article-header time without trusting sidebar timestamps."""
    hostname = urlsplit(page_url).hostname
    if hostname is None or hostname.casefold().removeprefix("www.") != "c114.com.cn":
        return ()
    return tuple(match.group(1) for match in _C114_ARTICLE_HEADER_DATE.finditer(html_text))


def _visible_publication_dates(html_text: str) -> tuple[str, ...]:
    """Read dates only when surrounding visible text explicitly labels them as publication time."""
    plain_text = html.unescape(re.sub(r"<[^>]+>", " ", html_text))
    normalized = " ".join(plain_text.split())
    return tuple(match.group(1) for match in _VISIBLE_PUBLICATION_DATE.finditer(normalized))


def _visible_source_header_publication_dates(html_text: str) -> tuple[str, ...]:
    """Read an article-header date only when it is immediately identified by 来源."""
    plain_text = html.unescape(re.sub(r"<[^>]+>", " ", html_text))
    normalized = " ".join(plain_text.split())
    return tuple(match.group(1) for match in _VISIBLE_SOURCE_HEADER_DATE.finditer(normalized))


def _json_ld_published_dates(value: object) -> list[str]:
    """Collect datePublished values without treating arbitrary JSON as trusted code."""
    if isinstance(value, list):
        return [date for item in value for date in _json_ld_published_dates(item)]
    if not isinstance(value, dict):
        return []
    dates = [value["datePublished"]] if isinstance(value.get("datePublished"), str) else []
    for child in value.values():
        dates.extend(_json_ld_published_dates(child))
    return dates


def _parse_publication_datetime(
    raw_value: str, *, default_timezone: tzinfo = UTC
) -> datetime | None:
    """Parse a standard HTML/JSON-LD timestamp without depending on host timezone."""
    normalized = raw_value.strip()
    if not normalized:
        return None
    chinese_match = _CHINESE_DATE.fullmatch(normalized)
    if chinese_match is not None:
        groups = chinese_match.groupdict()
        normalized = (
            f"{groups['year']}-{int(groups['month']):02d}-{int(groups['day']):02d}"
            f"T{int(groups['hour'] or 0):02d}:{int(groups['minute'] or 0):02d}"
            f":{int(groups['second'] or 0):02d}"
        )
    else:
        numeric_match = _NUMERIC_DATE.fullmatch(normalized)
        if numeric_match is not None:
            groups = numeric_match.groupdict()
            normalized = (
                f"{groups['year']}-{int(groups['month']):02d}-{int(groups['day']):02d}"
                f"T{int(groups['hour'] or 0):02d}:{int(groups['minute'] or 0):02d}"
                f":{int(groups['second'] or 0):02d}{groups['timezone'] or ''}"
            )
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=default_timezone)
    return parsed.astimezone(UTC)


def _looks_like_access_challenge(html_text: str) -> bool:
    """Reject short challenge documents before text extraction can mistake them for articles."""
    plain_text = re.sub(r"<[^>]+>", " ", html_text)
    normalized = " ".join(plain_text.casefold().split())
    return len(normalized) <= 5_000 and any(marker in normalized for marker in _CHALLENGE_MARKERS)
