"""Application service that persists deterministic processing decisions through repositories."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, date
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session, sessionmaker

from dailycast.core.hashes import sha256_text
from dailycast.core.time import Clock
from dailycast.db.models import Article, ArticleStatus, NewsEventStatus
from dailycast.db.repositories import ArticleRepository, NewsEventRepository
from dailycast.db.transactions import UnitOfWork
from dailycast.news.clustering import cluster_articles
from dailycast.news.deduplication import deduplicate_articles
from dailycast.news.filtering import filter_articles
from dailycast.news.types import (
    DeduplicationResult,
    FilterResult,
    ProcessableArticle,
    ProcessingPolicy,
)


@dataclass(frozen=True, slots=True)
class PersistedClusterResult:
    """Persisted events returned to the clustering pipeline checkpoint."""

    event_ids: tuple[int, ...]
    clustered_article_ids: tuple[int, ...]


class NewsProcessor:
    """Apply deterministic rules without exposing SQLAlchemy sessions to pipeline steps."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        policy: ProcessingPolicy,
        *,
        clock: Clock | None = None,
        timezone: str = "Asia/Shanghai",
    ) -> None:
        self._session_factory = session_factory
        self._policy = policy
        self._clock = clock or Clock()
        self._timezone = ZoneInfo(timezone)

    def filter(
        self, article_ids: tuple[int, ...], *, minimum_max_age_hours: int | None = None
    ) -> FilterResult:
        """Persist `eligible` or `filtered` status for the supplied Article checkpoint set."""
        snapshots = self._load_articles(article_ids)
        policy = self._policy
        if minimum_max_age_hours is not None:
            source_max_age_hours = {
                source_id: max(max_age_hours, minimum_max_age_hours)
                for source_id, max_age_hours in policy.source_max_age_hours.items()
            }
            policy = replace(
                policy,
                max_age_hours=max(policy.max_age_hours, minimum_max_age_hours),
                source_max_age_hours=source_max_age_hours,
            )
        decision = filter_articles(snapshots, policy, self._clock.now())
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            articles = ArticleRepository(unit.session)
            for article_id in decision.eligible_article_ids:
                article = articles.get(article_id)
                if article is not None:
                    articles.update(
                        article,
                        status=ArticleStatus.ELIGIBLE,
                        filter_reason=None,
                        duplicate_of_article_id=None,
                        news_event_id=None,
                    )
            for article_id, reason in decision.filtered_reasons.items():
                article = articles.get(article_id)
                if article is not None:
                    articles.update(
                        article,
                        status=ArticleStatus.FILTERED,
                        filter_reason=reason,
                        duplicate_of_article_id=None,
                        news_event_id=None,
                    )
        return decision

    def deduplicate(self, article_ids: tuple[int, ...]) -> DeduplicationResult:
        """Persist SimHash and duplicate mappings for currently eligible Article IDs."""
        snapshots = self._load_articles(article_ids, status=ArticleStatus.ELIGIBLE)
        decision = deduplicate_articles(tuple(snapshots), self._policy)
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            articles = ArticleRepository(unit.session)
            for article_id in decision.primary_article_ids:
                article = articles.get(article_id)
                if article is not None:
                    articles.update(
                        article,
                        status=ArticleStatus.ELIGIBLE,
                        filter_reason=None,
                        duplicate_of_article_id=None,
                        simhash=decision.simhashes.get(article_id),
                        news_event_id=None,
                    )
            for article_id, primary_article_id in decision.duplicate_of_article_ids.items():
                article = articles.get(article_id)
                if article is not None:
                    articles.update(
                        article,
                        status=ArticleStatus.DUPLICATE,
                        filter_reason=decision.reasons[article_id],
                        duplicate_of_article_id=primary_article_id,
                        simhash=decision.simhashes.get(article_id),
                        news_event_id=None,
                    )
        return decision

    def deduplicate_in_memory(self, article_ids: tuple[int, ...]) -> DeduplicationResult:
        """Reuse deterministic duplicate rules without persisting any Article mutation.

        Briefing selection shares Article rows with the podcast workflow. It needs
        primary evidence only, so it must not mark the losing Article as duplicate
        or persist a SimHash as the regular processing pipeline does.
        """
        snapshots = self._load_articles(article_ids, status=ArticleStatus.ELIGIBLE)
        # A report must not disappear because a template-heavy publisher produces a
        # SimHash collision. For briefing-local selection, only high textual overlap
        # proves a near duplicate; exact URL/content/title rules are unchanged.
        return deduplicate_articles(
            tuple(snapshots),
            self._policy,
            require_jaccard_for_near_duplicate=True,
        )

    def cluster(self, article_ids: tuple[int, ...]) -> PersistedClusterResult:
        """Create or refresh deterministic NewsEvents and assign their primary Articles."""
        snapshots = self._load_articles(article_ids, status=ArticleStatus.ELIGIBLE)
        clusters = cluster_articles(tuple(snapshots), self._policy)
        by_id = {article.id: article for article in snapshots}
        event_ids: list[int] = []
        clustered_article_ids: list[int] = []
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            articles = ArticleRepository(unit.session)
            events = NewsEventRepository(unit.session)
            for cluster in clusters:
                members = [by_id[article_id] for article_id in cluster.article_ids]
                representative = by_id[cluster.representative_article_id]
                event_date = self._event_date(representative)
                event_key = f"{event_date.isoformat()}:{min(cluster.article_ids)}"
                values = self._event_values(event_key, event_date, representative, members)
                event = events.get_by_event_key(event_key)
                event = events.create(**values) if event is None else events.update(event, **values)
                event_ids.append(event.id)
                for article_id in cluster.article_ids:
                    article = articles.get(article_id)
                    if article is not None:
                        articles.update(article, news_event_id=event.id)
                        clustered_article_ids.append(article.id)
        return PersistedClusterResult(tuple(event_ids), tuple(sorted(clustered_article_ids)))

    def _load_articles(
        self, article_ids: tuple[int, ...], *, status: ArticleStatus | None = None
    ) -> list[ProcessableArticle]:
        """Detach required Article/Source fields inside a short read transaction."""
        if not article_ids:
            return []
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            article_rows = ArticleRepository(unit.session).list_by_ids(article_ids)
            snapshots: list[ProcessableArticle] = []
            for article in article_rows:
                if status is not None and article.status != status:
                    continue
                snapshots.append(_snapshot(article))
            return snapshots

    def _event_date(self, article: ProcessableArticle) -> date:
        """Use representative publication time in configured business timezone for event keys."""
        value = article.published_at or article.discovered_at
        utc_value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return utc_value.astimezone(self._timezone).date()

    def _event_values(
        self,
        event_key: str,
        event_date: date,
        representative: ProcessableArticle,
        members: list[ProcessableArticle],
    ) -> dict[str, object]:
        """Create the persisted NewsEvent projection for one deterministic cluster."""
        published_values = [
            article.published_at for article in members if article.published_at is not None
        ]
        signature = sha256_text(
            ":".join(str(article.id) for article in sorted(members, key=lambda item: item.id))
        )
        return {
            "event_key": event_key,
            "event_date": event_date,
            "representative_article_id": representative.id,
            "title": representative.title,
            "summary": _event_summary(representative),
            "status": NewsEventStatus.CANDIDATE,
            "first_published_at": min(published_values) if published_values else None,
            "last_published_at": max(published_values) if published_values else None,
            "article_count": len(members),
            "source_count": len({article.source_id for article in members}),
            "deterministic_score": 0.0,
            "risk_flags_json": json.dumps([], separators=(",", ":")),
            "cluster_algorithm": self._policy.cluster_algorithm,
            "cluster_version": self._policy.cluster_version,
            "cluster_threshold": self._policy.similarity_threshold,
            "cluster_signature": signature,
        }


def _snapshot(article: Article) -> ProcessableArticle:
    """Build an immutable rule input while the SQLAlchemy relationship is still available."""
    return ProcessableArticle(
        id=article.id,
        source_id=article.source_id,
        source_priority=article.source.priority,
        url_hash=article.url_hash,
        title_hash=article.title_hash,
        content_hash=article.content_hash,
        title=article.title,
        summary=article.summary,
        content_text=article.content_text,
        language=article.language,
        published_at=article.published_at,
        discovered_at=article.discovered_at,
    )


def _event_summary(article: ProcessableArticle) -> str | None:
    """Store a bounded deterministic event summary without attempting editorial generation."""
    source = article.summary or article.content_text
    return source[:500] if source else None
