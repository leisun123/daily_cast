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
    assert revision.current == ("0002_task_run_waiting_action",)


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
                text(
                    """
                    INSERT INTO task_runs (
                        id, task_type, business_key, idempotency_key, trigger_type, status,
                        pipeline_version, config_fingerprint, config_snapshot_json, request_json,
                        created_at, updated_at
                    ) VALUES (
                        'legacy-task', 'daily_generate', 'daily:legacy', 'legacy-key', 'manual',
                        'queued', 'test-v1', :fingerprint, '{}', '{}', :created_at, :updated_at
                    )
                    """
                ),
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
