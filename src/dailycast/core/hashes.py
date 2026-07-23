"""Stable SHA-256 helper functions."""

from hashlib import sha256


def sha256_bytes(value: bytes) -> str:
    """Return the lowercase SHA-256 hex digest for bytes."""
    return sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    """Return the lowercase SHA-256 hex digest for UTF-8 text."""
    return sha256_bytes(value.encode("utf-8"))
