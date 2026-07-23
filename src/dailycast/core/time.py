"""Time abstraction used by runtime infrastructure."""

from datetime import UTC, datetime


class Clock:
    """Provide UTC timestamps without binding callers to a global function."""

    def now(self) -> datetime:
        """Return the current timezone-aware UTC time."""
        return datetime.now(UTC)
