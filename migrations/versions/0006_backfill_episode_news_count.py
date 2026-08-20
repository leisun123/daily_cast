"""Backfill existing episode topic counts after production metrics rollout.

Revision ID: 0006_backfill_episode_news_count
Revises: 0005_production_experience_metrics
Create Date: 2026-07-24
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0006_backfill_episode_news_count"
down_revision = "0005_production_experience_metrics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Derive historical counts from immutable EpisodeItem rows without changing editorial data."""
    op.get_bind().execute(sa.text("""
            UPDATE episodes
            SET news_count = (
                SELECT COUNT(*)
                FROM episode_items
                WHERE episode_items.episode_id = episodes.id
            )
            """))


def downgrade() -> None:
    """Keep derived counts intact because dropping the previous schema removes the column."""
