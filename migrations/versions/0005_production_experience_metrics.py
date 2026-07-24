"""Persist production-facing episode and task metrics.

Revision ID: 0005_production_experience_metrics
Revises: 0004_tts_preprocess_identity
Create Date: 2026-07-24
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0005_production_experience_metrics"
down_revision = "0004_tts_preprocess_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add additive counters while preserving existing Episode duration semantics."""
    connection = op.get_bind()
    is_sqlite = connection.dialect.name == "sqlite"
    if is_sqlite:
        _drop_interrupted_batch_table(connection, "episodes")
        _drop_interrupted_batch_table(connection, "task_runs")
        connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
    try:
        with op.batch_alter_table("episodes") as batch_op:
            batch_op.add_column(
                sa.Column("news_count", sa.Integer(), nullable=False, server_default=sa.text("0"))
            )
            batch_op.add_column(sa.Column("generation_time_seconds", sa.Integer(), nullable=True))
            batch_op.create_check_constraint("news_count_nonnegative", "news_count >= 0")
            batch_op.create_check_constraint(
                "generation_time_seconds_nonnegative",
                "generation_time_seconds IS NULL OR generation_time_seconds >= 0",
            )
        with op.batch_alter_table("task_runs") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "cache_hit_count", sa.Integer(), nullable=False, server_default=sa.text("0")
                )
            )
            batch_op.create_check_constraint("cache_hit_count_nonnegative", "cache_hit_count >= 0")
    finally:
        if is_sqlite:
            connection.exec_driver_sql("PRAGMA foreign_keys = ON")
    if is_sqlite and connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall():
        msg = "foreign key check failed after production metrics migration"
        raise RuntimeError(msg)


def downgrade() -> None:
    """Remove the additive operational metrics during an explicit downgrade only."""
    connection = op.get_bind()
    is_sqlite = connection.dialect.name == "sqlite"
    if is_sqlite:
        connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
    try:
        with op.batch_alter_table("task_runs") as batch_op:
            batch_op.drop_constraint("cache_hit_count_nonnegative", type_="check")
            batch_op.drop_column("cache_hit_count")
        with op.batch_alter_table("episodes") as batch_op:
            batch_op.drop_constraint("generation_time_seconds_nonnegative", type_="check")
            batch_op.drop_constraint("news_count_nonnegative", type_="check")
            batch_op.drop_column("generation_time_seconds")
            batch_op.drop_column("news_count")
    finally:
        if is_sqlite:
            connection.exec_driver_sql("PRAGMA foreign_keys = ON")
    if is_sqlite and connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall():
        msg = "foreign key check failed after production metrics downgrade"
        raise RuntimeError(msg)


def _drop_interrupted_batch_table(connection: sa.Connection, table_name: str) -> None:
    """Discard only Alembic's stale batch table when the canonical table still exists."""
    temporary_name = f"_alembic_tmp_{table_name}"
    temporary_exists = connection.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (temporary_name,)
    ).scalar_one_or_none()
    if temporary_exists is None:
        return
    source_exists = connection.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,)
    ).scalar_one_or_none()
    if source_exists is None:
        msg = f"cannot recover interrupted {table_name} migration without canonical source table"
        raise RuntimeError(msg)
    connection.exec_driver_sql(f"DROP TABLE {temporary_name}")
