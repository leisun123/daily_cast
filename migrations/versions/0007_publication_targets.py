"""Add independent multi-platform publication targets.

Revision ID: 0007_publication_targets
Revises: 0006_backfill_episode_news_count
Create Date: 2026-07-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_publication_targets"
down_revision = "0006_backfill_episode_news_count"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create per-Episode target state without changing the atomic RSS Publication table."""
    op.create_table(
        "publication_targets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("episode_id", sa.Integer(), nullable=False),
        sa.Column(
            "platform",
            sa.Enum(
                "rss",
                "netease",
                "xiaoyuzhou",
                name="publicationplatform",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "publishing",
                "published",
                "needs_attention",
                "failed",
                name="publicationtargetstatus",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("remote_id", sa.Text(), nullable=True),
        sa.Column("remote_url", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name=op.f("ck_publication_targets_attempt_count_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["episode_id"],
            ["episodes.id"],
            name=op.f("fk_publication_targets_episode_id_episodes"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_publication_targets")),
        sa.UniqueConstraint(
            "episode_id",
            "platform",
            name="uq_publication_targets_episode_platform",
        ),
    )
    op.create_index(
        "ix_publication_targets_episode",
        "publication_targets",
        ["episode_id"],
        unique=False,
    )
    op.create_index(
        "ix_publication_targets_platform_status",
        "publication_targets",
        ["platform", "status"],
        unique=False,
    )
    op.create_index(
        "ix_publication_targets_status",
        "publication_targets",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    """Remove only Sprint 10 target state."""
    op.drop_index("ix_publication_targets_status", table_name="publication_targets")
    op.drop_index("ix_publication_targets_platform_status", table_name="publication_targets")
    op.drop_index("ix_publication_targets_episode", table_name="publication_targets")
    op.drop_table("publication_targets")
