"""Persist independent multi-platform distribution target lifecycles.

Revision ID: 0007_publication_targets
Revises: 0006_backfill_episode_news_count
Create Date: 2026-07-24
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0007_publication_targets"
down_revision = "0006_backfill_episode_news_count"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create per-platform rows and project existing RSS records without rewriting feed state."""
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
        sa.CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        sa.ForeignKeyConstraint(["episode_id"], ["episodes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "episode_id", "platform", name="uq_publication_targets_episode_platform"
        ),
    )
    op.create_index("ix_publication_targets_status", "publication_targets", ["status"])
    op.create_index("ix_publication_targets_platform", "publication_targets", ["platform"])
    op.create_index("ix_publication_targets_remote", "publication_targets", ["remote_id"])

    op.get_bind().execute(
        sa.text(
            """
            INSERT INTO publication_targets (
                episode_id, platform, status, remote_id, remote_url, last_error,
                attempt_count, created_at, updated_at
            )
            SELECT
                episode_id,
                'rss',
                status,
                feed_guid,
                public_audio_url,
                error_summary,
                attempt_count,
                created_at,
                updated_at
            FROM publications
            WHERE publisher_type = 'rss'
            """
        )
    )


def downgrade() -> None:
    """Drop the derived target rows; the original RSS publication table remains untouched."""
    op.drop_index("ix_publication_targets_remote", table_name="publication_targets")
    op.drop_index("ix_publication_targets_platform", table_name="publication_targets")
    op.drop_index("ix_publication_targets_status", table_name="publication_targets")
    op.drop_table("publication_targets")
