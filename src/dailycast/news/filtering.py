"""Deterministic Article eligibility checks performed before any model call."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from dailycast.news.types import FilterResult, ProcessableArticle, ProcessingPolicy

MISSING_CONTENT = "MISSING_CONTENT"
MISSING_PUBLISHED_TIME = "MISSING_PUBLISHED_TIME"
PUBLISHED_TOO_OLD = "PUBLISHED_TOO_OLD"
CONTENT_TOO_SHORT = "CONTENT_TOO_SHORT"


def filter_articles(
    articles: list[ProcessableArticle], policy: ProcessingPolicy, now: datetime
) -> FilterResult:
    """Return eligible Article IDs and stable reasons while preserving every input row."""
    eligible_article_ids: list[int] = []
    filtered_reasons: dict[int, str] = {}
    oldest_allowed = _as_utc(now) - timedelta(hours=policy.max_age_hours)
    for article in sorted(articles, key=lambda item: item.id):
        reason = _filter_reason(article, oldest_allowed, policy)
        if reason is None:
            eligible_article_ids.append(article.id)
        else:
            filtered_reasons[article.id] = reason
    return FilterResult(tuple(eligible_article_ids), filtered_reasons)


def _filter_reason(
    article: ProcessableArticle, oldest_allowed: datetime, policy: ProcessingPolicy
) -> str | None:
    """Evaluate rules in an auditable, stable precedence order."""
    if article.content_text is None or not article.content_text.strip():
        return MISSING_CONTENT
    if article.published_at is None:
        return MISSING_PUBLISHED_TIME
    if _as_utc(article.published_at) < oldest_allowed:
        return PUBLISHED_TOO_OLD
    if len(article.content_text) < policy.min_content_length:
        return CONTENT_TOO_SHORT
    return None


def _as_utc(value: datetime) -> datetime:
    """Normalize SQLite-returned naive timestamps before deterministic comparison."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
