"""Allow the internal web-research source collector.

Revision ID: 0008_web_research_source_kind
Revises: 0007_publication_targets
Create Date: 2026-08-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_web_research_source_kind"
down_revision = "0007_publication_targets"
branch_labels = None
depends_on = None

_OLD_SOURCE_KIND = sa.Enum(
    "rss", "html_list", name="sourcekind", native_enum=False, create_constraint=True
)
_NEW_SOURCE_KIND = sa.Enum(
    "rss",
    "html_list",
    "web_research",
    name="sourcekind",
    native_enum=False,
    create_constraint=True,
)


def upgrade() -> None:
    """Recreate SQLite's source-kind check constraint with the new collector kind."""
    _replace_source_kind_constraint(_OLD_SOURCE_KIND, _NEW_SOURCE_KIND)


def downgrade() -> None:
    """Refuse to erase configured research sources while restoring the old constraint."""
    has_research_source = op.get_bind().execute(
        sa.text("SELECT 1 FROM sources WHERE kind = 'web_research' LIMIT 1")
    ).scalar()
    if has_research_source is not None:
        msg = "cannot downgrade while web_research sources exist"
        raise RuntimeError(msg)
    _replace_source_kind_constraint(_NEW_SOURCE_KIND, _OLD_SOURCE_KIND)


def _replace_source_kind_constraint(
    existing_type: sa.Enum, target_type: sa.Enum
) -> None:
    """Rebuild SQLite's constrained table without breaking referencing Article rows."""
    connection = op.get_bind()
    is_sqlite = connection.dialect.name == "sqlite"
    if is_sqlite:
        _drop_interrupted_batch_table(connection)
        connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
    try:
        with op.batch_alter_table("sources", recreate="always") as batch_op:
            batch_op.alter_column(
                "kind",
                existing_type=existing_type,
                type_=target_type,
                existing_nullable=False,
            )
    finally:
        if is_sqlite:
            connection.exec_driver_sql("PRAGMA foreign_keys = ON")
    if is_sqlite and connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall():
        msg = "foreign key check failed after source kind migration"
        raise RuntimeError(msg)


def _drop_interrupted_batch_table(connection: sa.Connection) -> None:
    """Discard only a stale Alembic table when the canonical source table remains."""
    temporary_name = "_alembic_tmp_sources"
    temporary_exists = connection.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (temporary_name,)
    ).scalar_one_or_none()
    if temporary_exists is None:
        return
    source_exists = connection.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sources'"
    ).scalar_one_or_none()
    if source_exists is None:
        msg = "cannot recover interrupted sources migration without canonical sources table"
        raise RuntimeError(msg)
    connection.exec_driver_sql(f"DROP TABLE {temporary_name}")
