"""Stable content identities for deterministic news processing."""

from __future__ import annotations

import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dailycast.core.hashes import sha256_text


def normalize_url(value: str) -> str:
    """Canonicalize an absolute HTTP(S) URL without tracking or fragment components."""
    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or parsed.hostname is None:
        msg = "article URL must be absolute HTTP or HTTPS"
        raise ValueError(msg)
    if parsed.username is not None or parsed.password is not None:
        msg = "article URL must not include credentials"
        raise ValueError(msg)
    try:
        port = parsed.port
    except ValueError as error:
        msg = "article URL has an invalid port"
        raise ValueError(msg) from error
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    authority = host
    if port is not None and not _is_default_port(scheme, port):
        authority = f"{host}:{port}"
    query_items = [
        (key, item_value)
        for key, item_value in parse_qsl(parsed.query, keep_blank_values=True)
        if not _is_tracking_query_key(key)
    ]
    return urlunsplit((scheme, authority, parsed.path or "/", urlencode(sorted(query_items)), ""))


def normalize_title(value: str) -> str:
    """Canonicalize title text for an exact-title identity without display markup."""
    normalized = unicodedata.normalize("NFKC", value)
    without_punctuation = "".join(
        " " if unicodedata.category(character).startswith("P") else character
        for character in normalized
    )
    return " ".join(without_punctuation.split()).casefold()


def normalize_content(value: str) -> str:
    """Canonicalize extracted plain text for a stable content identity."""
    return " ".join(unicodedata.normalize("NFKC", value).split())


def url_hash(normalized_url: str) -> str:
    """Return the SHA-256 identity for a normalized URL."""
    return sha256_text(normalized_url)


def title_hash(normalized_title: str) -> str:
    """Return the SHA-256 identity for a normalized title."""
    return sha256_text(normalized_title)


def content_hash(normalized_content: str) -> str:
    """Return the SHA-256 identity for normalized extracted content."""
    return sha256_text(normalized_content)


def _is_default_port(scheme: str, port: int) -> bool:
    """Recognize default HTTP(S) ports omitted from URL identity."""
    return (scheme == "http" and port == 80) or (scheme == "https" and port == 443)


def _is_tracking_query_key(value: str) -> bool:
    """Recognize documented common tracking query keys case-insensitively."""
    lowered = value.lower()
    return lowered.startswith("utm_") or lowered in {"fbclid", "gclid"}
