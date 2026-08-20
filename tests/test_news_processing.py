"""Sprint 3B deterministic filtering, deduplication, and clustering tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from alembic import command
from sqlalchemy.orm import Session, sessionmaker

from dailycast.core.config import load_settings
from dailycast.core.hashes import sha256_text
from dailycast.db.models import Article, ArticleStatus, SourceKind
from dailycast.db.repositories import ArticleRepository, SourceRepository
from dailycast.db.revision import build_alembic_config
from dailycast.db.session import create_session_factory, create_sqlite_engine
from dailycast.db.transactions import UnitOfWork
from dailycast.news.clustering import cluster_articles
from dailycast.news.deduplication import deduplicate_articles
from dailycast.news.filtering import filter_articles
from dailycast.news.normalization import (
    content_hash,
    normalize_content,
    normalize_title,
    normalize_url,
    title_hash,
    url_hash,
)
from dailycast.news.service import NewsProcessor
from dailycast.news.types import ProcessableArticle, ProcessingPolicy

RATE_CUT_TEXT = "central bank interest rate cut inflation slowing policy decision " "markets react "
RATE_REDUCTION_TEXT = (
    "inflation cooling central bank announces rate reduction policy " "meeting markets react "
)


class FixedClock:
    """Return one deterministic timestamp in processing-service tests."""

    def __init__(self, value: datetime) -> None:
        self._value = value

    def now(self) -> datetime:
        """Return the configured UTC test time."""
        return self._value


def upgraded_factory(app_config_path: Path) -> tuple[Any, sessionmaker[Session]]:
    """Create an isolated database through the same Alembic route as production."""
    settings = load_settings(config_path=app_config_path)
    ini_path = Path(__file__).resolve().parents[1] / "alembic.ini"
    command.upgrade(
        build_alembic_config(ini_path=ini_path, database_url=settings.database.url), "head"
    )
    engine = create_sqlite_engine(settings.database)
    return engine, create_session_factory(engine)


def create_source(factory: sessionmaker[Session], *, source_id: str, priority: int = 50) -> None:
    """Persist a source that supplies deterministic source-quality metadata."""
    now = datetime(2026, 7, 22, tzinfo=UTC)
    with UnitOfWork(factory) as unit:
        assert unit.session is not None
        SourceRepository(unit.session).create(
            id=source_id,
            name=source_id,
            kind=SourceKind.RSS,
            entry_url=f"https://{source_id}.example.test/rss",
            normalized_entry_url=f"https://{source_id}.example.test/rss",
            priority=priority,
            config_json="{}",
            created_at=now,
            updated_at=now,
        )


def create_article(
    factory: sessionmaker[Session],
    *,
    source_id: str,
    unique: str,
    content: str | None,
    published_at: datetime | None,
    title: str | None = None,
) -> Article:
    """Persist one extracted Article with stable normalized fields for processing tests."""
    title_value = title or f"Article {unique}"
    normalized_url = normalize_url(f"https://news.example.test/{unique}")
    normalized_title = normalize_title(title_value)
    normalized_content = normalize_content(content) if content is not None else None
    now = datetime(2026, 7, 22, tzinfo=UTC)
    with UnitOfWork(factory) as unit:
        assert unit.session is not None
        article = ArticleRepository(unit.session).upsert(
            source_id=source_id,
            external_id=unique,
            url=normalized_url,
            normalized_url=normalized_url,
            url_hash=url_hash(normalized_url),
            title=title_value,
            normalized_title=normalized_title,
            title_hash=title_hash(normalized_title),
            summary=None,
            content_text=normalized_content,
            content_hash=content_hash(normalized_content) if normalized_content else None,
            language="en",
            published_at=published_at,
            discovered_at=now,
            status=ArticleStatus.EXTRACTED,
            metadata_json="{}",
            created_at=now,
            updated_at=now,
        )
        return article


def processable_article(
    article_id: int,
    *,
    title: str,
    content: str,
    source_id: str = "source-a",
    source_priority: int = 50,
    published_at: datetime | None = None,
    url_identity: str | None = None,
) -> ProcessableArticle:
    """Build a pure-algorithm input with internally consistent normalized hashes."""
    timestamp = published_at or datetime(2026, 7, 22, 12, tzinfo=UTC)
    normalized_url = normalize_url(url_identity or f"https://news.example.test/{article_id}")
    normalized_title = normalize_title(title)
    normalized_content = normalize_content(content)
    return ProcessableArticle(
        id=article_id,
        source_id=source_id,
        source_priority=source_priority,
        url_hash=url_hash(normalized_url),
        title_hash=title_hash(normalized_title),
        content_hash=content_hash(normalized_content),
        title=title,
        summary=None,
        content_text=normalized_content,
        language="en",
        published_at=timestamp,
        discovered_at=timestamp,
    )


def snapshot_from_article(article: Article) -> ProcessableArticle:
    """Build a pure filtering input from one persisted test Article."""
    return ProcessableArticle(
        id=article.id,
        source_id=article.source_id,
        source_priority=50,
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


def test_normalization_and_hashes_are_stable() -> None:
    """URL, title, and body variations resolve to one deterministic identity."""
    first_url = normalize_url("HTTPS://Example.com:443/path?b=2&utm_source=rss&a=1#part")
    second_url = normalize_url("https://example.com/path?a=1&b=2")

    assert first_url == second_url == "https://example.com/path?a=1&b=2"
    assert normalize_title("Ａ  Major—Update!") == normalize_title("a major update")
    assert normalize_content("A\u00a0  story\nwith\tspaces") == "A story with spaces"
    assert url_hash(first_url) == url_hash(second_url)
    assert title_hash(normalize_title("Same title")) == sha256_text(normalize_title("Same title"))
    assert content_hash(normalize_content("Same body")) == sha256_text(
        normalize_content("Same body")
    )


def test_filtering_rejects_old_short_and_missing_content_articles(app_config_path: Path) -> None:
    """Filtering retains audit rows while assigning stable reasons to ineligible content."""
    now = datetime(2026, 7, 22, 12, tzinfo=UTC)
    engine, factory = upgraded_factory(app_config_path)
    try:
        create_source(factory, source_id="source-a")
        fresh = create_article(
            factory,
            source_id="source-a",
            unique="fresh",
            content="reliable coverage " * 20,
            published_at=now - timedelta(hours=1),
        )
        old = create_article(
            factory,
            source_id="source-a",
            unique="old",
            content="reliable coverage " * 20,
            published_at=now - timedelta(hours=48),
        )
        short = create_article(
            factory,
            source_id="source-a",
            unique="short",
            content="too short",
            published_at=now - timedelta(hours=1),
        )
        missing = create_article(
            factory,
            source_id="source-a",
            unique="missing",
            content=None,
            published_at=now - timedelta(hours=1),
        )

        policy = ProcessingPolicy(max_age_hours=24, min_content_length=100)
        decision = filter_articles(
            [
                snapshot_from_article(fresh),
                snapshot_from_article(old),
                snapshot_from_article(short),
                ProcessableArticle(
                    id=missing.id,
                    source_id="source-a",
                    source_priority=50,
                    url_hash=missing.url_hash,
                    title_hash=missing.title_hash,
                    content_hash=None,
                    title=missing.title,
                    summary=None,
                    content_text=None,
                    language="en",
                    published_at=missing.published_at,
                    discovered_at=missing.discovered_at,
                ),
            ],
            policy,
            now,
        )

        assert decision.eligible_article_ids == (fresh.id,)
        assert decision.filtered_reasons == {
            old.id: "PUBLISHED_TOO_OLD",
            short.id: "CONTENT_TOO_SHORT",
            missing.id: "MISSING_CONTENT",
        }
    finally:
        engine.dispose()


def test_filtering_keeps_recent_recruitment_notices_for_the_watch_window() -> None:
    """Recruitment notices remain eligible for the longer user-requested tracking window."""
    now = datetime(2026, 7, 22, 12, tzinfo=UTC)
    recruitment_notice = processable_article(
        1,
        source_id="changzhou-public-recruitment",
        title="2026年常州市事业单位公开招聘工作人员公告",
        content="常州市事业单位公开招聘工作人员公告，报名资格和时间安排详见正文。" * 12,
        published_at=now - timedelta(days=6),
    )

    decision = filter_articles(
        [recruitment_notice],
        ProcessingPolicy(max_age_hours=36, min_content_length=300),
        now,
    )

    assert decision.eligible_article_ids == (recruitment_notice.id,)
    assert decision.filtered_reasons == {}


def test_deduplication_marks_exact_and_near_duplicate_articles() -> None:
    """URL/content equality and high-overlap text select one deterministic quality winner."""
    now = datetime(2026, 7, 22, 12, tzinfo=UTC)
    same_url_low_priority = processable_article(
        1,
        title="Storm closes airport",
        content="airport closure weather disruption emergency response " * 12,
        source_priority=20,
        url_identity="https://news.example.test/storm",
    )
    same_url_high_priority = processable_article(
        2,
        title="Storm closes airport",
        content="airport closure weather disruption emergency response " * 14,
        source_priority=80,
        url_identity="https://news.example.test/storm",
    )
    same_content = processable_article(
        3,
        title="Airport closure report",
        content=same_url_high_priority.content_text or "",
        source_priority=60,
    )
    near_duplicate = processable_article(
        4,
        title="Storm shuts airport as weather disrupts flights",
        content=(same_url_high_priority.content_text or "") + " updated",
        source_priority=40,
        published_at=now + timedelta(hours=1),
    )

    result = deduplicate_articles(
        (same_url_low_priority, same_url_high_priority, same_content, near_duplicate),
        ProcessingPolicy(near_duplicate_jaccard_threshold=0.75),
    )

    assert result.primary_article_ids == (same_url_high_priority.id,)
    assert result.duplicate_of_article_ids == {
        same_url_low_priority.id: same_url_high_priority.id,
        same_content.id: same_url_high_priority.id,
        near_duplicate.id: same_url_high_priority.id,
    }
    assert result.reasons[same_url_low_priority.id] == "DUPLICATE_URL_HASH"
    assert result.reasons[same_content.id] == "DUPLICATE_CONTENT_HASH"
    assert result.reasons[near_duplicate.id] == "DUPLICATE_NEAR_CONTENT"


def test_clustering_groups_one_event_and_separates_another() -> None:
    """TF-IDF character n-gram graph components create stable event memberships."""
    same_event_a = processable_article(
        1,
        title="Central bank cuts interest rates after inflation slows",
        content=RATE_CUT_TEXT * 10,
        source_id="source-a",
        source_priority=70,
    )
    same_event_b = processable_article(
        2,
        title="Interest rate reduction announced as inflation cools",
        content=RATE_REDUCTION_TEXT * 10,
        source_id="source-b",
        source_priority=90,
    )
    different_event = processable_article(
        3,
        title="Wildfire evacuation begins near coastal national park",
        content="wildfire evacuation coastal national park emergency crews residents safety " * 10,
        source_id="source-c",
    )

    clusters = cluster_articles(
        (same_event_a, same_event_b, different_event),
        ProcessingPolicy(similarity_threshold=0.5),
    )

    assert [cluster.article_ids for cluster in clusters] == [(1, 2), (3,)]
    assert clusters[0].representative_article_id == 2


def test_news_processor_persists_filter_deduplication_and_events(app_config_path: Path) -> None:
    """The persistence service maps deterministic decisions back to Article and NewsEvent rows."""
    now = datetime(2026, 7, 22, 12, tzinfo=UTC)
    engine, factory = upgraded_factory(app_config_path)
    try:
        create_source(factory, source_id="source-a", priority=80)
        create_source(factory, source_id="source-b", priority=60)
        first = create_article(
            factory,
            source_id="source-a",
            unique="rate-a",
            title="Central bank cuts interest rates after inflation slows",
            content=RATE_CUT_TEXT * 12,
            published_at=now - timedelta(hours=2),
        )
        second = create_article(
            factory,
            source_id="source-b",
            unique="rate-b",
            title="Interest rate reduction announced as inflation cools",
            content=RATE_REDUCTION_TEXT * 12,
            published_at=now - timedelta(hours=1),
        )

        processor = NewsProcessor(
            factory,
            ProcessingPolicy(min_content_length=100, similarity_threshold=0.28),
            clock=FixedClock(now),
        )
        filtered = processor.filter((first.id, second.id))
        deduplicated = processor.deduplicate(filtered.eligible_article_ids)
        clustered = processor.cluster(deduplicated.primary_article_ids)

        assert filtered.eligible_article_ids == (first.id, second.id)
        assert deduplicated.primary_article_ids == (first.id, second.id)
        assert len(clustered.event_ids) == 1
        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            first_row = ArticleRepository(unit.session).get(first.id)
            second_row = ArticleRepository(unit.session).get(second.id)
            assert first_row is not None and second_row is not None
            assert first_row.status == ArticleStatus.ELIGIBLE
            assert first_row.news_event_id == second_row.news_event_id
            assert first_row.news_event_id == clustered.event_ids[0]
            assert first_row.news_event is not None
            assert first_row.news_event.article_count == 2
            assert first_row.news_event.cluster_algorithm == "tfidf_char"
            assert first_row.news_event.cluster_version == "1"
    finally:
        engine.dispose()
