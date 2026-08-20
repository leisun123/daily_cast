"""Alembic baseline tests without application ORM models."""

from pathlib import Path

from alembic import command
from sqlalchemy import text

from dailycast.core.config import load_settings
from dailycast.db.revision import build_alembic_config, inspect_revision
from dailycast.db.session import create_sqlite_engine


def test_initial_schema_revision_reaches_head(app_config_path: Path) -> None:
    """The initial migration creates the V1 schema and reaches head."""
    settings = load_settings(config_path=app_config_path)
    ini_path = Path(__file__).resolve().parents[1] / "alembic.ini"
    config = build_alembic_config(ini_path=ini_path, database_url=settings.database.url)
    command.upgrade(config, "head")

    engine = create_sqlite_engine(settings.database)
    try:
        revision = inspect_revision(
            engine=engine,
            ini_path=ini_path,
            database_url=settings.database.url,
        )
    finally:
        engine.dispose()

    assert revision.is_current is True
    assert revision.current == ("0006_backfill_episode_news_count",)


def test_production_metrics_migration_backfills_existing_episode_news_count(
    app_config_path: Path,
) -> None:
    """Existing published Episodes derive their new count from immutable EpisodeItem snapshots."""
    settings = load_settings(config_path=app_config_path)
    ini_path = Path(__file__).resolve().parents[1] / "alembic.ini"
    config = build_alembic_config(ini_path=ini_path, database_url=settings.database.url)
    command.upgrade(config, "0004_tts_preprocess_identity")

    engine = create_sqlite_engine(settings.database)
    try:
        with engine.begin() as connection:
            connection.execute(text("""
                    INSERT INTO sources (id, name, kind, entry_url, normalized_entry_url,
                                         config_json, created_at, updated_at)
                    VALUES ('metric-source', 'Metric source', 'rss', 'https://example.test/rss',
                            'https://example.test/rss', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """))
            connection.execute(
                text("""
                    INSERT INTO articles (source_id, url, normalized_url, url_hash, title,
                                              normalized_title, title_hash, discovered_at, status,
                                              metadata_json, created_at, updated_at)
                    VALUES ('metric-source', 'https://example.test/article',
                            'https://example.test/article', :url_hash, 'Metric article',
                                'metric article', :title_hash, CURRENT_TIMESTAMP, 'extracted', '{}',
                                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """),
                {"url_hash": "a" * 64, "title_hash": "b" * 64},
            )
            article_id = connection.execute(text("SELECT id FROM articles")).scalar_one()
            connection.execute(
                text("""
                    INSERT INTO news_events (event_key, event_date, representative_article_id,
                                             title, status, article_count, source_count,
                                             deterministic_score, risk_flags_json,
                                             cluster_algorithm, cluster_version, cluster_threshold,
                                             cluster_signature,
                                             created_at, updated_at)
                    VALUES ('metric-event', '2026-07-22', :article_id, 'Metric event', 'selected',
                            1, 1, 0, '[]', 'tfidf_char', 'test-v1', 0.58, :cluster_signature,
                            CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """),
                {"article_id": article_id, "cluster_signature": "c" * 64},
            )
            event_id = connection.execute(text("SELECT id FROM news_events")).scalar_one()
            connection.execute(text("""
                    INSERT INTO episodes (public_id, episode_date, edition, status,
                                          script_revision, audio_version, lock_version,
                                          created_at, updated_at)
                    VALUES ('metric-episode', '2026-07-22', 'daily', 'published',
                            1, 1, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """))
            episode_id = connection.execute(text("SELECT id FROM episodes")).scalar_one()
            for position in (1,):
                connection.execute(
                    text("""
                        INSERT INTO episode_items (episode_id, news_event_id, position,
                                                   event_title_snapshot, selection_reason_snapshot,
                                                   score_snapshot_json, source_article_ids_json,
                                                   created_at, updated_at)
                        VALUES (:episode_id, :event_id, :position, 'Metric event', 'Selected',
                                '{}', '[]', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                        """),
                    {"episode_id": episode_id, "event_id": event_id, "position": position},
                )
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_sqlite_engine(settings.database)
    try:
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT news_count FROM episodes WHERE public_id = 'metric-episode'")
                ).scalar_one()
                == 1
            )
    finally:
        engine.dispose()


def test_waiting_action_migration_upgrades_existing_task_run_schema(app_config_path: Path) -> None:
    """An existing V1 database accepts the non-failed waiting-action terminal state."""
    settings = load_settings(config_path=app_config_path)
    ini_path = Path(__file__).resolve().parents[1] / "alembic.ini"
    config = build_alembic_config(ini_path=ini_path, database_url=settings.database.url)
    command.upgrade(config, "0001_initial_schema")

    engine = create_sqlite_engine(settings.database)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("""
                    INSERT INTO task_runs (
                        id, task_type, business_key, idempotency_key, trigger_type, status,
                        pipeline_version, config_fingerprint, config_snapshot_json, request_json,
                        created_at, updated_at
                    ) VALUES (
                        'legacy-task', 'daily_generate', 'daily:legacy', 'legacy-key', 'manual',
                        'queued', 'test-v1', :fingerprint, '{}', '{}', :created_at, :updated_at
                    )
                    """),
                {
                    "fingerprint": "a" * 64,
                    "created_at": "2026-07-23 00:00:00",
                    "updated_at": "2026-07-23 00:00:00",
                },
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")

    engine = create_sqlite_engine(settings.database)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE task_runs SET status = 'waiting_action' WHERE id = 'legacy-task'")
            )
    finally:
        engine.dispose()


def test_reliability_migration_preserves_task_steps_referenced_by_llm_artifacts(
    app_config_path: Path,
) -> None:
    """SQLite batch migration must not drop a referenced TaskStep while foreign keys are enabled."""
    settings = load_settings(config_path=app_config_path)
    ini_path = Path(__file__).resolve().parents[1] / "alembic.ini"
    config = build_alembic_config(ini_path=ini_path, database_url=settings.database.url)
    command.upgrade(config, "0002_task_run_waiting_action")

    engine = create_sqlite_engine(settings.database)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("""
                    INSERT INTO task_runs (
                        id, task_type, business_key, idempotency_key, trigger_type, status,
                        pipeline_version, config_fingerprint, config_snapshot_json, request_json,
                        created_at, updated_at
                    ) VALUES (
                        'referenced-task', 'daily_generate', 'daily:referenced', 'referenced-key',
                        'manual', 'succeeded', 'test-v1', :hash, '{}', '{}', :now, :now
                    )
                    """),
                {"hash": "a" * 64, "now": "2026-07-24 00:00:00"},
            )
            connection.execute(
                text("""
                    INSERT INTO task_steps (
                        task_run_id, step_name, step_order, attempt, status, details_json,
                        created_at, updated_at
                    ) VALUES (
                        'referenced-task', 'ranking', 1, 1, 'succeeded', '{}', :now, :now
                    )
                    """),
                {"now": "2026-07-24 00:00:00"},
            )
            step_id = connection.execute(
                text("SELECT id FROM task_steps WHERE task_run_id = 'referenced-task'")
            ).scalar_one()
            connection.execute(
                text("""
                    INSERT INTO llm_artifacts (
                        operation, provider, model, prompt_version, schema_version,
                        generation_config_hash, input_hash, output_json, output_hash,
                        created_by_task_run_id, created_by_task_step_id, created_at
                    ) VALUES (
                        'score_events', 'fake', 'fake-model', 'v1', 'v1', :config_hash,
                        :input_hash, '{}', :output_hash, 'referenced-task', :step_id, :now
                    )
                    """),
                {
                    "config_hash": "b" * 64,
                    "input_hash": "c" * 64,
                    "output_hash": "d" * 64,
                    "step_id": step_id,
                    "now": "2026-07-24 00:00:00",
                },
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")

    engine = create_sqlite_engine(settings.database)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT COUNT(*) FROM llm_artifacts")).scalar_one() == 1
            assert (
                connection.execute(text("SELECT tts_character_count FROM task_steps")).scalar_one()
                == 0
            )
    finally:
        engine.dispose()


def test_reliability_migration_recovers_from_its_own_interrupted_sqlite_batch_table(
    app_config_path: Path,
) -> None:
    """A prior failed 0003 attempt leaves a removable temp table beside the canonical source."""
    settings = load_settings(config_path=app_config_path)
    ini_path = Path(__file__).resolve().parents[1] / "alembic.ini"
    config = build_alembic_config(ini_path=ini_path, database_url=settings.database.url)
    command.upgrade(config, "0002_task_run_waiting_action")

    engine = create_sqlite_engine(settings.database)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("CREATE TABLE _alembic_tmp_task_steps AS SELECT * FROM task_steps")
            )
    finally:
        engine.dispose()


def test_tts_preprocess_migration_backfills_existing_audio_segments(app_config_path: Path) -> None:
    """The additive cache-identity column upgrades historical audio rows safely."""
    settings = load_settings(config_path=app_config_path)
    ini_path = Path(__file__).resolve().parents[1] / "alembic.ini"
    config = build_alembic_config(ini_path=ini_path, database_url=settings.database.url)
    command.upgrade(config, "0003_reliability_hardening")

    engine = create_sqlite_engine(settings.database)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("""
                    INSERT INTO episodes (public_id, episode_date, status, created_at, updated_at)
                    VALUES ('legacy-audio-episode', '2026-07-24', 'draft', :now, :now)
                    """),
                {"now": "2026-07-24 00:00:00"},
            )
            episode_id = connection.execute(
                text("SELECT id FROM episodes WHERE public_id = 'legacy-audio-episode'")
            ).scalar_one()
            connection.execute(
                text("""
                    INSERT INTO audio_segments (
                        episode_id, script_revision, segment_index, segmenter_version, text,
                        text_hash, cache_key, provider, model, voice, provider_config_hash,
                        status, created_at, updated_at
                    ) VALUES (
                        :episode_id, 1, 0, 'v1', '历史片段', :text_hash, :cache_key,
                        'edge_tts', 'edge-tts', 'zh-CN-XiaoxiaoNeural', :provider_hash,
                        'succeeded', :now, :now
                    )
                    """),
                {
                    "episode_id": episode_id,
                    "text_hash": "a" * 64,
                    "cache_key": "b" * 64,
                    "provider_hash": "c" * 64,
                    "now": "2026-07-24 00:00:00",
                },
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")

    engine = create_sqlite_engine(settings.database)
    try:
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT tts_preprocess_hash FROM audio_segments")
                ).scalar_one()
                == "0" * 64
            )
    finally:
        engine.dispose()


def test_tts_preprocess_migration_recovers_from_its_own_interrupted_batch_table(
    app_config_path: Path,
) -> None:
    """A stale Alembic audio temp table does not block retrying the additive 0004 migration."""
    settings = load_settings(config_path=app_config_path)
    ini_path = Path(__file__).resolve().parents[1] / "alembic.ini"
    config = build_alembic_config(ini_path=ini_path, database_url=settings.database.url)
    command.upgrade(config, "0003_reliability_hardening")

    engine = create_sqlite_engine(settings.database)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("CREATE TABLE _alembic_tmp_audio_segments AS SELECT * FROM audio_segments")
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")

    engine = create_sqlite_engine(settings.database)
    try:
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM sqlite_master "
                        "WHERE type = 'table' AND name = '_alembic_tmp_audio_segments'"
                    )
                ).scalar_one()
                == 0
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")

    engine = create_sqlite_engine(settings.database)
    try:
        with engine.connect() as connection:
            temporary_table_count = connection.execute(
                text(
                    "SELECT COUNT(*) FROM sqlite_master "
                    "WHERE type = 'table' AND name = '_alembic_tmp_task_steps'"
                )
            ).scalar_one()
            assert temporary_table_count == 0
            assert connection.execute(text("SELECT COUNT(*) FROM task_steps")).scalar_one() == 0
    finally:
        engine.dispose()
