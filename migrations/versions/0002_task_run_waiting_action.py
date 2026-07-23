"""add waiting-action TaskRun status

Revision ID: 0002_task_run_waiting_action
Revises: 0001_initial_schema
Create Date: 2026-07-23 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_task_run_waiting_action"
down_revision: str | Sequence[str] | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECK_NAME = "taskrunstatus"
_OLD_CHECK = (
    "status IN ('queued', 'running', 'succeeded', 'succeeded_with_warnings', "
    "'failed', 'timed_out', 'interrupted', 'cancelled')"
)
_NEW_CHECK = (
    "status IN ('queued', 'running', 'waiting_action', 'succeeded', "
    "'succeeded_with_warnings', 'failed', 'timed_out', 'interrupted', 'cancelled')"
)


def upgrade() -> None:
    """Recreate the SQLite check constraint while preserving every TaskRun row and index."""
    _replace_status_check(_NEW_CHECK)


def downgrade() -> None:
    """Refuse to discard waiting-action audit rows during an explicit downgrade."""
    connection = op.get_bind()
    count = connection.exec_driver_sql(
        "SELECT COUNT(*) FROM task_runs WHERE status = 'waiting_action'"
    ).scalar_one()
    if count:
        msg = "cannot downgrade while task_runs contain waiting_action rows"
        raise RuntimeError(msg)
    _replace_status_check(_OLD_CHECK)


def _replace_status_check(expression: str) -> None:
    """Use Alembic batch mode because SQLite cannot alter a table CHECK constraint in place."""
    connection = op.get_bind()
    is_sqlite = connection.dialect.name == "sqlite"
    if is_sqlite:
        connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
    try:
        with op.batch_alter_table("task_runs", recreate="always") as batch_op:
            batch_op.drop_constraint(_CHECK_NAME, type_="check")
            batch_op.create_check_constraint(_CHECK_NAME, expression)
    finally:
        if is_sqlite:
            connection.exec_driver_sql("PRAGMA foreign_keys = ON")
    if is_sqlite and connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall():
        msg = "foreign key check failed after task_runs status migration"
        raise RuntimeError(msg)
