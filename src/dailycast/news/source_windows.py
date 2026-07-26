"""Per-source freshness defaults for recurring notice trackers."""

from __future__ import annotations

from types import MappingProxyType

RECRUITMENT_SOURCE_IDS = frozenset(
    {
        "changzhou-public-recruitment",
        "jiangsu-civil-service-notices",
    }
)
DEFAULT_SOURCE_MAX_AGE_HOURS = MappingProxyType(
    {source_id: 14 * 24 for source_id in RECRUITMENT_SOURCE_IDS}
)
