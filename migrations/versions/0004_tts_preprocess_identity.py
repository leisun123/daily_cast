"""Persist the TTS preprocessing identity used by each cached audio segment.

Revision ID: 0004_tts_preprocess_identity
Revises: 0003_reliability_hardening
Create Date: 2026-07-24
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0004_tts_preprocess_identity"
down_revision = "0003_reliability_hardening"
branch_labels = None
depends_on = None

_LEGACY_PREPROCESS_HASH = "0" * 64


def upgrade() -> None:
    """Add explicit preprocessing identity and rebuild the cache lookup index for SQLite."""
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        _drop_interrupted_batch_table(connection)
    with op.batch_alter_table("audio_segments") as batch_op:
        batch_op.add_column(
            sa.Column(
                "tts_preprocess_hash",
                sa.String(length=64),
                nullable=False,
                server_default=sa.text(f"'{_LEGACY_PREPROCESS_HASH}'"),
            )
        )
        batch_op.create_check_constraint(
            "tts_preprocess_hash_length", "length(tts_preprocess_hash) = 64"
        )
        batch_op.drop_index("ix_audio_segments_cache")
        batch_op.create_index(
            "ix_audio_segments_cache",
            ["cache_key", "provider_config_hash", "tts_preprocess_hash", "status"],
            unique=False,
        )


def downgrade() -> None:
    """Remove the additive identity and restore the exact earlier cache index."""
    with op.batch_alter_table("audio_segments") as batch_op:
        batch_op.drop_index("ix_audio_segments_cache")
        batch_op.create_index(
            "ix_audio_segments_cache",
            ["cache_key", "provider_config_hash", "status"],
            unique=False,
        )
        batch_op.drop_constraint("tts_preprocess_hash_length", type_="check")
        batch_op.drop_column("tts_preprocess_hash")


def _drop_interrupted_batch_table(connection: sa.Connection) -> None:
    """Discard only Alembic's stale temp table when the canonical source table remains."""
    temporary_exists = connection.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = '_alembic_tmp_audio_segments'"
    ).scalar_one_or_none()
    if temporary_exists is None:
        return
    source_exists = connection.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'audio_segments'"
    ).scalar_one_or_none()
    if source_exists is None:
        msg = "cannot recover interrupted audio_segments migration without canonical audio_segments table"
        raise RuntimeError(msg)
    connection.exec_driver_sql("DROP TABLE _alembic_tmp_audio_segments")
