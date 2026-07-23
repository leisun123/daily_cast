"""Bounded HTTP fetching and trafilatura extraction with SSRF protections."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn, Protocol
from urllib.parse import urljoin, urlsplit

import httpx
import trafilatura

from dailycast.sources.contracts import ExtractedArticle, SourceError


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
        content_type = response.headers.get("content-type", "").lower().split(";", maxsplit=1)[0]
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
        html_text = response.content.decode("utf-8", errors="replace")
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
        )
