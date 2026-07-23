"""Alembic baseline tests without application ORM models."""

from pathlib import Path

from alembic import command

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
    assert revision.current == ("0001_initial_schema",)
