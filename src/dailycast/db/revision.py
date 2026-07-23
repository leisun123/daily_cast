"""Alembic revision inspection without creating database schema at application startup."""

from dataclasses import dataclass
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine


@dataclass(frozen=True)
class RevisionStatus:
    """Current database revisions compared with the migration script heads."""

    current: tuple[str, ...]
    expected: tuple[str, ...]

    @property
    def is_current(self) -> bool:
        """Return true only when database and migration graph have identical heads."""
        return self.current == self.expected


def build_alembic_config(*, ini_path: Path, database_url: str) -> Config:
    """Create an Alembic config that targets the configured SQLite database."""
    config = Config(str(ini_path))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def inspect_revision(*, engine: Engine, ini_path: Path, database_url: str) -> RevisionStatus:
    """Read migration heads; this never upgrades or creates a database revision."""
    config = build_alembic_config(ini_path=ini_path, database_url=database_url)
    script = ScriptDirectory.from_config(config)
    expected = tuple(sorted(script.get_heads()))
    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        current = tuple(sorted(context.get_current_heads()))
    return RevisionStatus(current=current, expected=expected)
