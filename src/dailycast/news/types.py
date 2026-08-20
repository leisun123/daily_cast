"""Typed values shared by the deterministic news-processing rules and service."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from dailycast.news.source_windows import DEFAULT_SOURCE_MAX_AGE_HOURS


@dataclass(frozen=True, slots=True)
class ProcessingPolicy:
    """Explicit V1 bounds and thresholds for deterministic processing."""

    max_age_hours: int = 36
    source_max_age_hours: dict[str, int] = field(
        default_factory=lambda: dict(DEFAULT_SOURCE_MAX_AGE_HOURS)
    )
    min_content_length: int = 300
    title_duplicate_window_hours: int = 72
    near_duplicate_window_hours: int = 72
    near_duplicate_hamming_distance: int = 3
    near_duplicate_jaccard_threshold: float = 0.90
    cluster_time_window_hours: int = 72
    similarity_threshold: float = 0.58
    cluster_algorithm: str = "tfidf_char"
    cluster_version: str = "1"


@dataclass(frozen=True, slots=True)
class ProcessableArticle:
    """Detached Article fields required by deterministic rules, ordered by durable ID."""

    id: int
    source_id: str
    source_priority: int
    url_hash: str
    title_hash: str
    content_hash: str | None
    title: str
    summary: str | None
    content_text: str | None
    language: str | None
    published_at: datetime | None
    discovered_at: datetime


@dataclass(frozen=True, slots=True)
class FilterResult:
    """The Article IDs that pass deterministic rules plus stable rejection codes."""

    eligible_article_ids: tuple[int, ...]
    filtered_reasons: dict[int, str]


@dataclass(frozen=True, slots=True)
class DeduplicationResult:
    """Deterministic primary choices, duplicate mappings, reasons, and SimHash values."""

    primary_article_ids: tuple[int, ...]
    duplicate_of_article_ids: dict[int, int]
    reasons: dict[int, str]
    simhashes: dict[int, str]


@dataclass(frozen=True, slots=True)
class ArticleCluster:
    """One connected-component event candidate and its quality-selected representative."""

    article_ids: tuple[int, ...]
    representative_article_id: int


@dataclass(frozen=True, slots=True)
class ClusterResult:
    """Persisted event IDs and the Article IDs that were assigned to them."""

    event_ids: tuple[int, ...]
    clustered_article_ids: tuple[int, ...]
