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
    """Add the durable provider-character counter to each task checkpoint."""
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


def downgrade() -> None:
    """Remove the additive accounting column for an explicit downgrade only."""
    with op.batch_alter_table("task_steps") as batch_op:
        batch_op.drop_constraint("tts_character_count_nonnegative", type_="check")
        batch_op.drop_column("tts_character_count")
