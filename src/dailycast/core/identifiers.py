"""Identifier generation helpers."""

from uuid import UUID, uuid4


class UUIDGenerator:
    """Create UUIDs for request correlation and future persisted identifiers."""

    def new(self) -> UUID:
        """Return a random UUID version 4."""
        return uuid4()
