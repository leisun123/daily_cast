"""Add TaskStep TTS accounting required by reliability hardening.

Revision ID: 0003_reliability_hardening
Revises: 0002_task_run_waiting_action
Create Date: 2026-07-24
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0003_reliability_hardening"
down_revision = "0002_task_run_waiting_action"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the durable provider-character counter without dropping referenced TaskStep rows."""
    connection = op.get_bind()
    is_sqlite = connection.dialect.name == "sqlite"
    if is_sqlite:
        _drop_interrupted_batch_table(connection)
        connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
    try:
        with op.batch_alter_table("task_steps") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "tts_character_count",
                    sa.Integer(),
                    nullable=False,
                    server_default=sa.text("0"),
                )
            )
            batch_op.create_check_constraint(
                "tts_character_count_nonnegative", "tts_character_count >= 0"
            )
    finally:
        if is_sqlite:
            connection.exec_driver_sql("PRAGMA foreign_keys = ON")
    if is_sqlite and connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall():
        msg = "foreign key check failed after task_steps usage migration"
        raise RuntimeError(msg)


def downgrade() -> None:
    """Remove the additive accounting column for an explicit downgrade only."""
    connection = op.get_bind()
    is_sqlite = connection.dialect.name == "sqlite"
    if is_sqlite:
        connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
    try:
        with op.batch_alter_table("task_steps") as batch_op:
            batch_op.drop_constraint("tts_character_count_nonnegative", type_="check")
            batch_op.drop_column("tts_character_count")
    finally:
        if is_sqlite:
            connection.exec_driver_sql("PRAGMA foreign_keys = ON")
    if is_sqlite and connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall():
        msg = "foreign key check failed after task_steps usage downgrade"
        raise RuntimeError(msg)


def _drop_interrupted_batch_table(connection: sa.Connection) -> None:
    """Remove only Alembic's stale temporary table when the canonical source table still exists."""
    temporary_exists = connection.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = '_alembic_tmp_task_steps'"
    ).scalar_one_or_none()
    if temporary_exists is None:
        return
    source_exists = connection.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'task_steps'"
    ).scalar_one_or_none()
    if source_exists is None:
        msg = "cannot recover interrupted task_steps migration without canonical task_steps table"
        raise RuntimeError(msg)
    connection.exec_driver_sql("DROP TABLE _alembic_tmp_task_steps")
