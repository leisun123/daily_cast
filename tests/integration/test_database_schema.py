"""Integration checks for the V1 SQLite schema created through Alembic."""

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from dailycast.core.config import load_settings
from dailycast.db.models import (
    Article,
    AudioSegment,
    Episode,
    LLMArtifact,
    NewsEvent,
    PublicationPlatform,
    PublicationTarget,
    PublicationTargetStatus,
    Source,
    TaskRun,
    TaskStep,
)
from dailycast.db.revision import build_alembic_config, inspect_revision
from dailycast.db.session import create_session_factory, create_sqlite_engine


def now() -> datetime:
    """Return a deterministic-enough UTC timestamp for test rows."""
    return datetime.now(UTC)


def upgrade_empty_database(app_config_path: Path):
    """Upgrade an isolated empty SQLite database only through Alembic."""
    settings = load_settings(config_path=app_config_path)
    ini_path = Path(__file__).resolve().parents[2] / "alembic.ini"
    config = build_alembic_config(ini_path=ini_path, database_url=settings.database.url)
    command.upgrade(config, "head")
    engine = create_sqlite_engine(settings.database)
    return engine, ini_path, settings.database.url


def create_source() -> Source:
    """Build the minimum valid source record."""
    timestamp = now()
    return Source(
        id="test-source",
        name="Test Source",
        kind="rss",
        entry_url="https://example.test/feed.xml",
        normalized_entry_url="https://example.test/feed.xml",
        config_json="{}",
        created_at=timestamp,
        updated_at=timestamp,
    )


def create_article(source_id: str, url_hash: str) -> Article:
    """Build a minimum article record with a stable URL identity."""
    timestamp = now()
    return Article(
        source_id=source_id,
        url=f"https://example.test/{url_hash}",
        normalized_url=f"https://example.test/{url_hash}",
        url_hash=url_hash,
        title="Article title",
        normalized_title="article title",
        title_hash="b" * 64,
        discovered_at=timestamp,
        status="discovered",
        metadata_json="{}",
        created_at=timestamp,
        updated_at=timestamp,
    )


def test_upgrade_empty_database_creates_full_schema(app_config_path: Path) -> None:
    """Alembic creates every V1 table and reaches the current revision head."""
    engine, ini_path, database_url = upgrade_empty_database(app_config_path)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
            assert connection.execute(text("PRAGMA journal_mode")).scalar_one() == "wal"
            assert connection.execute(text("PRAGMA busy_timeout")).scalar_one() == 5000
            tables = set(
                connection.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))
            )
        assert {
            "sources",
            "articles",
            "news_events",
            "episodes",
            "episode_items",
            "task_runs",
            "task_steps",
            "llm_artifacts",
            "audio_segments",
            "publications",
            "publication_targets",
        }.issubset({name for (name,) in tables})
        revision = inspect_revision(
            engine=engine,
            ini_path=ini_path,
            database_url=database_url,
        )
        assert revision.is_current is True
        assert revision.current == ("0007_publication_targets",)
    finally:
        engine.dispose()


def test_sqlite_safety_pragmas_apply_to_each_connection(app_config_path: Path) -> None:
    """Every pooled connection keeps foreign keys and busy timeout after one-time WAL setup."""
    engine, _, _ = upgrade_empty_database(app_config_path)
    try:
        with engine.connect() as first_connection, engine.connect() as second_connection:
            for connection in (first_connection, second_connection):
                assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
                assert connection.execute(text("PRAGMA busy_timeout")).scalar_one() == 5000
                assert connection.execute(text("PRAGMA journal_mode")).scalar_one() == "wal"
    finally:
        engine.dispose()


def test_unique_constraints_reject_duplicate_v1_identities(app_config_path: Path) -> None:
    """Primary V1 uniqueness rules reject duplicate source, article, episode, and artifact rows."""
    engine, _, _ = upgrade_empty_database(app_config_path)
    factory = create_session_factory(engine)
    session = factory()
    timestamp = now()
    try:
        source = create_source()
        session.add(source)
        session.flush()

        session.expunge(source)
        session.add(
            Source(
                id="test-source",
                name="Duplicate Source",
                kind="rss",
                entry_url="https://duplicate.test/feed.xml",
                normalized_entry_url="https://duplicate.test/feed.xml",
                config_json="{}",
                created_at=timestamp,
                updated_at=timestamp,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

        session.add(create_source())
        article = create_article("test-source", "a" * 64)
        session.add(article)
        session.commit()

        session.add(create_article("test-source", "a" * 64))
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

        episode = Episode(
            public_id="11111111-1111-1111-1111-111111111111",
            episode_date=date(2026, 7, 22),
            edition="daily",
            status="draft",
            created_at=timestamp,
            updated_at=timestamp,
        )
        session.add(episode)
        session.commit()
        session.add(
            Episode(
                public_id="22222222-2222-2222-2222-222222222222",
                episode_date=date(2026, 7, 22),
                edition="daily",
                status="draft",
                created_at=timestamp,
                updated_at=timestamp,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

        session.add(
            PublicationTarget(
                episode_id=episode.id,
                platform=PublicationPlatform.RSS,
                status=PublicationTargetStatus.PENDING,
                created_at=timestamp,
                updated_at=timestamp,
            )
        )
        session.commit()
        session.add(
            PublicationTarget(
                episode_id=episode.id,
                platform=PublicationPlatform.RSS,
                status=PublicationTargetStatus.PENDING,
                created_at=timestamp,
                updated_at=timestamp,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

        task_run = TaskRun(
            id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            task_type="daily_generate",
            business_key="daily:2026-07-22:daily",
            idempotency_key="task-1",
            trigger_type="manual",
            status="queued",
            pipeline_version="1",
            config_fingerprint="c" * 64,
            config_snapshot_json="{}",
            request_json="{}",
            created_at=timestamp,
            updated_at=timestamp,
        )
        session.add(task_run)
        session.flush()
        task_step = TaskStep(
            task_run_id=task_run.id,
            step_name="ranking",
            step_order=1,
            attempt=1,
            status="succeeded",
            details_json="{}",
            created_at=timestamp,
            updated_at=timestamp,
        )
        session.add(task_step)
        session.flush()
        artifact_fields = dict(
            operation="score_events",
            provider="openai_compatible",
            model="test-model",
            prompt_version="1",
            schema_version="1",
            generation_config_hash="d" * 64,
            input_hash="e" * 64,
            output_json="{}",
            output_hash="f" * 64,
            created_by_task_run_id=task_run.id,
            created_by_task_step_id=task_step.id,
            created_at=timestamp,
        )
        session.add(LLMArtifact(**artifact_fields))
        session.commit()
        session.add(LLMArtifact(**{**artifact_fields, "generation_config_hash": "1" * 64}))
        session.commit()
        session.add(LLMArtifact(**artifact_fields))
        with pytest.raises(IntegrityError):
            session.flush()
    finally:
        session.rollback()
        session.close()
        engine.dispose()


def test_foreign_keys_json_checks_and_tts_hash_checks_are_enforced(app_config_path: Path) -> None:
    """SQLite enforces the documented foreign keys and JSON/hash CHECK constraints."""
    engine, _, _ = upgrade_empty_database(app_config_path)
    factory = create_session_factory(engine)
    session = factory()
    timestamp = now()
    try:
        session.add(create_article("missing-source", "a" * 64))
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

        session.add(
            PublicationTarget(
                episode_id=999_999,
                platform=PublicationPlatform.NETEASE,
                status=PublicationTargetStatus.PENDING,
                created_at=timestamp,
                updated_at=timestamp,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

        source = create_source()
        session.add(source)
        session.flush()
        article = create_article(source.id, "a" * 64)
        article.metadata_json = "not-json"
        session.add(article)
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

        episode = Episode(
            public_id="33333333-3333-3333-3333-333333333333",
            episode_date=date(2026, 7, 22),
            status="draft",
            created_at=timestamp,
            updated_at=timestamp,
        )
        session.add(episode)
        session.flush()
        session.add(
            AudioSegment(
                episode_id=episode.id,
                script_revision=0,
                segment_index=0,
                segmenter_version="1",
                text="测试",
                text_hash="a" * 64,
                cache_key="too-short",
                provider="openai_compatible",
                model="tts-1",
                voice="alloy",
                provider_config_hash="b" * 64,
                tts_preprocess_hash="c" * 64,
                status="pending",
                created_at=timestamp,
                updated_at=timestamp,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
    finally:
        session.rollback()
        session.close()
        engine.dispose()


def test_article_event_cycle_can_be_created_in_documented_order(app_config_path: Path) -> None:
    """A representative article is inserted before the event, then attached to that event."""
    engine, _, _ = upgrade_empty_database(app_config_path)
    factory = create_session_factory(engine)
    session = factory()
    timestamp = now()
    try:
        source = create_source()
        article = create_article(source.id, "a" * 64)
        session.add_all([source, article])
        session.flush()
        event = NewsEvent(
            event_key=f"2026-07-22:{article.id}",
            event_date=date(2026, 7, 22),
            representative_article_id=article.id,
            title=article.title,
            status="candidate",
            risk_flags_json="[]",
            cluster_algorithm="tfidf_char",
            cluster_version="1",
            cluster_threshold=0.58,
            cluster_signature="c" * 64,
            created_at=timestamp,
            updated_at=timestamp,
        )
        session.add(event)
        session.flush()
        article.news_event_id = event.id
        session.commit()
        assert article.news_event_id == event.id
    finally:
        session.rollback()
        session.close()
        engine.dispose()


def test_reliability_migration_adds_nonnegative_task_step_tts_usage(
    app_config_path: Path,
) -> None:
    """Alembic 0003 supplies the task-step accounting column and its SQLite CHECK."""
    engine, _, _ = upgrade_empty_database(app_config_path)
    try:
        with engine.begin() as connection:
            columns = {row[1] for row in connection.execute(text("PRAGMA table_info(task_steps)"))}
            assert "tts_character_count" in columns
            with pytest.raises(IntegrityError):
                connection.execute(
                    text(
                        """
                        INSERT INTO task_runs (
                            id, task_type, business_key, idempotency_key, trigger_type, status,
                            pipeline_version, config_fingerprint, config_snapshot_json,
                            request_json, created_at, updated_at
                        ) VALUES (
                            'tts-check-task', 'daily_generate', 'tts-check', 'tts-check-key',
                            'manual', 'queued', 'test', :fingerprint, '{}', '{}', :created, :updated
                        )
                        """
                    ),
                    {"fingerprint": "a" * 64, "created": now(), "updated": now()},
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO task_steps (
                            task_run_id, step_name, step_order, attempt, status, details_json,
                            tts_character_count
                        ) VALUES ('tts-check-task', 'tts', 1, 1, 'succeeded', '{}', -1)
                        """
                    )
                )
    finally:
        engine.dispose()


def test_tts_preprocess_identity_is_persisted_on_audio_segments(app_config_path: Path) -> None:
    """Audio cache rows record the explicit preprocessing policy that shaped the spoken input."""
    engine, _, _ = upgrade_empty_database(app_config_path)
    try:
        with engine.connect() as connection:
            columns = {
                row[1] for row in connection.execute(text("PRAGMA table_info(audio_segments)"))
            }
        assert "tts_preprocess_hash" in columns
    finally:
        engine.dispose()
