"""initial schema

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-07-22 14:13:57.164000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0001_initial_schema"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "articles",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("normalized_url", sa.Text(), nullable=False),
        sa.Column("url_hash", sa.String(length=64), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("normalized_title", sa.Text(), nullable=False),
        sa.Column("title_hash", sa.String(length=64), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("content_text", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("simhash", sa.String(length=16), nullable=True),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "published_at_inferred", sa.Boolean(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("content_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "discovered",
                "fetching",
                "extracted",
                "eligible",
                "filtered",
                "duplicate",
                "extraction_failed",
                name="articlestatus",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("filter_reason", sa.Text(), nullable=True),
        sa.Column("duplicate_of_article_id", sa.Integer(), nullable=True),
        sa.Column("news_event_id", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.Text(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "json_valid(metadata_json)", name=op.f("ck_articles_metadata_json_valid")
        ),
        sa.CheckConstraint(
            "published_at_inferred IN (0, 1)",
            name=op.f("ck_articles_published_at_inferred_boolean"),
        ),
        sa.ForeignKeyConstraint(
            ["duplicate_of_article_id"],
            ["articles.id"],
            name=op.f("fk_articles_duplicate_of_article_id_articles"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["news_event_id"],
            ["news_events.id"],
            name=op.f("fk_articles_news_event_id_news_events"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name=op.f("fk_articles_source_id_sources"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_articles")),
        sa.UniqueConstraint("url_hash", name=op.f("uq_articles_url_hash")),
    )
    op.create_index("ix_articles_content_hash", "articles", ["content_hash"], unique=False)
    op.create_index("ix_articles_duplicate", "articles", ["duplicate_of_article_id"], unique=False)
    op.create_index("ix_articles_event", "articles", ["news_event_id"], unique=False)
    op.create_index("ix_articles_source", "articles", ["source_id"], unique=False)
    op.create_index(
        "ix_articles_published", "articles", [sa.text("published_at DESC")], unique=False
    )
    op.create_index("ix_articles_status", "articles", ["status"], unique=False)
    op.create_index("ix_articles_title_hash", "articles", ["title_hash"], unique=False)
    op.create_index(
        "uq_articles_source_external",
        "articles",
        ["source_id", "external_id"],
        unique=True,
        sqlite_where=sa.text("external_id IS NOT NULL"),
    )
    op.create_table(
        "episodes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(length=36), nullable=False),
        sa.Column("episode_date", sa.Date(), nullable=False),
        sa.Column("edition", sa.Text(), server_default=sa.text("'daily'"), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "draft",
                "review_required",
                "approved",
                "publishing",
                "published",
                "failed",
                name="episodestatus",
                native_enum=False,
                create_constraint=True,
            ),
            server_default=sa.text("'draft'"),
            nullable=False,
        ),
        sa.Column("lock_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("outline_json", sa.Text(), nullable=True),
        sa.Column("script_json", sa.Text(), nullable=True),
        sa.Column("script_text", sa.Text(), nullable=True),
        sa.Column("script_revision", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("script_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "script_origin",
            sa.Enum(
                "generated",
                "edited",
                name="scriptorigin",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=True,
        ),
        sa.Column("review_json", sa.Text(), nullable=True),
        sa.Column("target_duration_seconds", sa.Integer(), nullable=True),
        sa.Column("actual_duration_ms", sa.Integer(), nullable=True),
        sa.Column("audio_version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("audio_manifest_hash", sa.String(length=64), nullable=True),
        sa.Column("draft_audio_path", sa.Text(), nullable=True),
        sa.Column("draft_audio_sha256", sa.String(length=64), nullable=True),
        sa.Column("approved_script_revision", sa.Integer(), nullable=True),
        sa.Column("approved_audio_version", sa.Integer(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "outline_json IS NULL OR json_valid(outline_json)",
            name=op.f("ck_episodes_outline_json_valid"),
        ),
        sa.CheckConstraint(
            "review_json IS NULL OR json_valid(review_json)",
            name=op.f("ck_episodes_review_json_valid"),
        ),
        sa.CheckConstraint(
            "script_json IS NULL OR json_valid(script_json)",
            name=op.f("ck_episodes_script_json_valid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_episodes")),
        sa.UniqueConstraint("episode_date", "edition", name="uq_episodes_date_edition"),
        sa.UniqueConstraint("public_id", name=op.f("uq_episodes_public_id")),
    )
    op.create_index("ix_episodes_date", "episodes", [sa.text("episode_date DESC")], unique=False)
    op.create_index(
        "ix_episodes_published", "episodes", [sa.text("published_at DESC")], unique=False
    )
    op.create_index("ix_episodes_status", "episodes", ["status"], unique=False)
    op.create_table(
        "news_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_key", sa.Text(), nullable=False),
        sa.Column("event_date", sa.Date(), nullable=False),
        sa.Column("representative_article_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "candidate",
                "scored",
                "selected",
                "rejected",
                name="newseventstatus",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("first_published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("article_count", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("source_count", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("deterministic_score", sa.Float(), server_default=sa.text("0"), nullable=False),
        sa.Column("importance_score", sa.Float(), nullable=True),
        sa.Column("relevance_score", sa.Float(), nullable=True),
        sa.Column("confidence_score", sa.Float(), nullable=True),
        sa.Column("selection_reason", sa.Text(), nullable=True),
        sa.Column("risk_flags_json", sa.Text(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("score_json", sa.Text(), nullable=True),
        sa.Column("cluster_algorithm", sa.Text(), nullable=False),
        sa.Column("cluster_version", sa.Text(), nullable=False),
        sa.Column("cluster_threshold", sa.Float(), nullable=False),
        sa.Column("cluster_signature", sa.String(length=64), nullable=False),
        sa.Column("llm_model", sa.Text(), nullable=True),
        sa.Column("llm_prompt_version", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "article_count >= 1", name=op.f("ck_news_events_article_count_positive")
        ),
        sa.CheckConstraint(
            "confidence_score IS NULL OR confidence_score BETWEEN 0 AND 100",
            name=op.f("ck_news_events_confidence_score_range"),
        ),
        sa.CheckConstraint(
            "importance_score IS NULL OR importance_score BETWEEN 0 AND 100",
            name=op.f("ck_news_events_importance_score_range"),
        ),
        sa.CheckConstraint(
            "json_valid(risk_flags_json)", name=op.f("ck_news_events_risk_flags_json_valid")
        ),
        sa.CheckConstraint(
            "relevance_score IS NULL OR relevance_score BETWEEN 0 AND 100",
            name=op.f("ck_news_events_relevance_score_range"),
        ),
        sa.CheckConstraint(
            "score_json IS NULL OR json_valid(score_json)",
            name=op.f("ck_news_events_score_json_valid"),
        ),
        sa.CheckConstraint("source_count >= 1", name=op.f("ck_news_events_source_count_positive")),
        sa.ForeignKeyConstraint(
            ["representative_article_id"],
            ["articles.id"],
            name=op.f("fk_news_events_representative_article_id_articles"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_news_events")),
        sa.UniqueConstraint("event_key", name=op.f("uq_news_events_event_key")),
    )
    op.create_index(
        "ix_news_events_representative", "news_events", ["representative_article_id"], unique=False
    )
    op.create_index("ix_news_events_signature", "news_events", ["cluster_signature"], unique=False)
    op.create_index(
        "ix_news_events_date", "news_events", [sa.text("event_date DESC")], unique=False
    )
    op.create_index(
        "ix_news_events_importance", "news_events", [sa.text("importance_score DESC")], unique=False
    )
    op.create_index("ix_news_events_status", "news_events", ["status"], unique=False)
    op.create_table(
        "sources",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "rss", "html_list", name="sourcekind", native_enum=False, create_constraint=True
            ),
            nullable=False,
        ),
        sa.Column("entry_url", sa.Text(), nullable=False),
        sa.Column("normalized_entry_url", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column("priority", sa.Integer(), server_default=sa.text("(50)"), nullable=False),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("config_json", sa.Text(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column(
            "request_timeout_seconds", sa.Integer(), server_default=sa.text("(20)"), nullable=False
        ),
        sa.Column(
            "max_items_per_run", sa.Integer(), server_default=sa.text("(50)"), nullable=False
        ),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.Column("last_error_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("enabled IN (0, 1)", name=op.f("ck_sources_enabled_boolean")),
        sa.CheckConstraint(
            "max_items_per_run BETWEEN 1 AND 500", name=op.f("ck_sources_max_items_per_run_range")
        ),
        sa.CheckConstraint("priority BETWEEN 0 AND 100", name=op.f("ck_sources_priority_range")),
        sa.CheckConstraint(
            "request_timeout_seconds BETWEEN 1 AND 120",
            name=op.f("ck_sources_request_timeout_seconds_range"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sources")),
        sa.UniqueConstraint("normalized_entry_url", name=op.f("uq_sources_normalized_entry_url")),
    )
    op.create_index(
        "ix_sources_enabled_priority",
        "sources",
        ["enabled", sa.text("priority DESC")],
        unique=False,
    )
    op.create_index("ix_sources_kind", "sources", ["kind"], unique=False)
    op.create_table(
        "audio_segments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("episode_id", sa.Integer(), nullable=False),
        sa.Column("script_revision", sa.Integer(), nullable=False),
        sa.Column("segment_index", sa.Integer(), nullable=False),
        sa.Column("segmenter_version", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_hash", sa.String(length=64), nullable=False),
        sa.Column("cache_key", sa.String(length=64), nullable=False),
        sa.Column("force_nonce", sa.Text(), nullable=True),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("voice", sa.Text(), nullable=False),
        sa.Column("speed", sa.Float(), server_default=sa.text("(1.0)"), nullable=False),
        sa.Column("format", sa.Text(), server_default=sa.text("'mp3'"), nullable=False),
        sa.Column("provider_config_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "synthesizing",
                "succeeded",
                "failed",
                "stale",
                name="audiosegmentstatus",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("audio_path", sa.Text(), nullable=True),
        sa.Column("mime_type", sa.Text(), nullable=True),
        sa.Column("byte_size", sa.Integer(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("provider_request_id", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(cache_key) = 64", name=op.f("ck_audio_segments_cache_key_length")
        ),
        sa.CheckConstraint(
            "length(provider_config_hash) = 64",
            name=op.f("ck_audio_segments_provider_config_hash_length"),
        ),
        sa.CheckConstraint(
            "segment_index >= 0", name=op.f("ck_audio_segments_segment_index_nonnegative")
        ),
        sa.ForeignKeyConstraint(
            ["episode_id"],
            ["episodes.id"],
            name=op.f("fk_audio_segments_episode_id_episodes"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audio_segments")),
        sa.UniqueConstraint(
            "episode_id",
            "script_revision",
            "segment_index",
            name="uq_audio_segments_episode_revision_index",
        ),
    )
    op.create_index(
        "ix_audio_segments_cache",
        "audio_segments",
        ["cache_key", "provider_config_hash", "status"],
        unique=False,
    )
    op.create_index(
        "ix_audio_segments_episode_revision_status",
        "audio_segments",
        ["episode_id", "script_revision", "status"],
        unique=False,
    )
    op.create_index("ix_audio_segments_sha", "audio_segments", ["sha256"], unique=False)
    op.create_table(
        "episode_items",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("episode_id", sa.Integer(), nullable=False),
        sa.Column("news_event_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("event_title_snapshot", sa.Text(), nullable=False),
        sa.Column("selection_reason_snapshot", sa.Text(), nullable=False),
        sa.Column("score_snapshot_json", sa.Text(), nullable=False),
        sa.Column("source_article_ids_json", sa.Text(), nullable=False),
        sa.Column("section_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "json_valid(score_snapshot_json)",
            name=op.f("ck_episode_items_score_snapshot_json_valid"),
        ),
        sa.CheckConstraint(
            "json_valid(source_article_ids_json)",
            name=op.f("ck_episode_items_source_article_ids_json_valid"),
        ),
        sa.CheckConstraint("position >= 1", name=op.f("ck_episode_items_position_positive")),
        sa.ForeignKeyConstraint(
            ["episode_id"],
            ["episodes.id"],
            name=op.f("fk_episode_items_episode_id_episodes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["news_event_id"],
            ["news_events.id"],
            name=op.f("fk_episode_items_news_event_id_news_events"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_episode_items")),
        sa.UniqueConstraint("episode_id", "news_event_id", name="uq_episode_items_episode_event"),
        sa.UniqueConstraint("episode_id", "position", name="uq_episode_items_episode_position"),
    )
    op.create_index("ix_episode_items_episode", "episode_items", ["episode_id"], unique=False)
    op.create_index("ix_episode_items_event", "episode_items", ["news_event_id"], unique=False)
    op.create_table(
        "publications",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("episode_id", sa.Integer(), nullable=False),
        sa.Column(
            "publisher_type",
            sa.Enum("rss", name="publishertype", native_enum=False, create_constraint=True),
            nullable=False,
        ),
        sa.Column("target_key", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "publishing",
                "published",
                "failed",
                name="publicationstatus",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("remote_id", sa.Text(), nullable=True),
        sa.Column("remote_url", sa.Text(), nullable=True),
        sa.Column("public_asset_path", sa.Text(), nullable=True),
        sa.Column("public_audio_url", sa.Text(), nullable=True),
        sa.Column("asset_sha256", sa.String(length=64), nullable=True),
        sa.Column("asset_byte_size", sa.Integer(), nullable=True),
        sa.Column("feed_guid", sa.Text(), nullable=True),
        sa.Column("response_summary_json", sa.Text(), nullable=True),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "response_summary_json IS NULL OR json_valid(response_summary_json)",
            name=op.f("ck_publications_response_summary_json_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["episode_id"],
            ["episodes.id"],
            name=op.f("fk_publications_episode_id_episodes"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_publications")),
        sa.UniqueConstraint(
            "episode_id",
            "publisher_type",
            "target_key",
            name="uq_publications_episode_publisher_target",
        ),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_publications_idempotency_key")),
    )
    op.create_index("ix_publications_remote", "publications", ["remote_id"], unique=False)
    op.create_index(
        "ix_publications_published", "publications", [sa.text("published_at DESC")], unique=False
    )
    op.create_index("ix_publications_status", "publications", ["status"], unique=False)
    op.create_index("ix_publications_type", "publications", ["publisher_type"], unique=False)
    op.create_table(
        "task_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "task_type",
            sa.Enum(
                "daily_generate",
                "publish",
                "regenerate_episode",
                "regenerate_segment",
                name="tasktype",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("business_key", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column(
            "trigger_type",
            sa.Enum(
                "manual",
                "scheduled",
                "retry",
                name="triggertype",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "running",
                "succeeded",
                "succeeded_with_warnings",
                "failed",
                "timed_out",
                "interrupted",
                "cancelled",
                name="taskrunstatus",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("current_step", sa.Text(), nullable=True),
        sa.Column("episode_id", sa.Integer(), nullable=True),
        sa.Column("parent_task_run_id", sa.String(length=36), nullable=True),
        sa.Column("pipeline_version", sa.Text(), nullable=False),
        sa.Column("config_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("config_snapshot_json", sa.Text(), nullable=False),
        sa.Column("request_json", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("warning_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("llm_call_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("llm_input_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("llm_output_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("tts_character_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("retryable", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("log_path", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "json_valid(config_snapshot_json)", name=op.f("ck_task_runs_config_snapshot_json_valid")
        ),
        sa.CheckConstraint(
            "json_valid(request_json)", name=op.f("ck_task_runs_request_json_valid")
        ),
        sa.CheckConstraint("retryable IN (0, 1)", name=op.f("ck_task_runs_retryable_boolean")),
        sa.ForeignKeyConstraint(
            ["episode_id"],
            ["episodes.id"],
            name=op.f("fk_task_runs_episode_id_episodes"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["parent_task_run_id"],
            ["task_runs.id"],
            name=op.f("fk_task_runs_parent_task_run_id_task_runs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_runs")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_task_runs_idempotency_key")),
    )
    op.create_index("ix_task_runs_episode", "task_runs", ["episode_id"], unique=False)
    op.create_index("ix_task_runs_heartbeat", "task_runs", ["heartbeat_at"], unique=False)
    op.create_index("ix_task_runs_parent", "task_runs", ["parent_task_run_id"], unique=False)
    op.create_index("ix_task_runs_created", "task_runs", [sa.text("created_at DESC")], unique=False)
    op.create_index("ix_task_runs_status", "task_runs", ["status"], unique=False)
    op.create_index("ix_task_runs_type", "task_runs", ["task_type"], unique=False)
    op.create_index(
        "uq_task_runs_active_business",
        "task_runs",
        ["business_key"],
        unique=True,
        sqlite_where=sa.text("status IN ('queued','running')"),
    )
    op.create_table(
        "task_steps",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_run_id", sa.String(length=36), nullable=False),
        sa.Column("step_name", sa.Text(), nullable=False),
        sa.Column("step_order", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "running",
                "succeeded",
                "succeeded_with_warnings",
                "failed",
                "skipped",
                name="taskstepstatus",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("input_count", sa.Integer(), nullable=True),
        sa.Column("output_count", sa.Integer(), nullable=True),
        sa.Column("warning_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("output_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("checkpoint_json", sa.Text(), nullable=True),
        sa.Column("details_json", sa.Text(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("artifact_path", sa.Text(), nullable=True),
        sa.Column("llm_call_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("llm_input_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("llm_output_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("retryable", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("attempt >= 1", name=op.f("ck_task_steps_attempt_positive")),
        sa.CheckConstraint(
            "checkpoint_json IS NULL OR json_valid(checkpoint_json)",
            name=op.f("ck_task_steps_checkpoint_json_valid"),
        ),
        sa.CheckConstraint(
            "json_valid(details_json)", name=op.f("ck_task_steps_details_json_valid")
        ),
        sa.CheckConstraint("retryable IN (0, 1)", name=op.f("ck_task_steps_retryable_boolean")),
        sa.ForeignKeyConstraint(
            ["task_run_id"],
            ["task_runs.id"],
            name=op.f("fk_task_steps_task_run_id_task_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_steps")),
        sa.UniqueConstraint("task_run_id", "id", name="uq_task_steps_run_id"),
        sa.UniqueConstraint(
            "task_run_id", "step_name", "attempt", name="uq_task_steps_run_name_attempt"
        ),
    )
    op.create_index("ix_task_steps_error", "task_steps", ["error_code"], unique=False)
    op.create_index(
        "ix_task_steps_run_order", "task_steps", ["task_run_id", "step_order"], unique=False
    )
    op.create_index("ix_task_steps_status", "task_steps", ["status"], unique=False)
    op.create_table(
        "llm_artifacts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "operation",
            sa.Enum(
                "score_events",
                "generate_outline",
                "generate_script",
                "generate_metadata",
                "review_script",
                name="llmoperation",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("generation_config_hash", sa.String(length=64), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("output_json", sa.Text(), nullable=False),
        sa.Column("output_hash", sa.String(length=64), nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("provider_request_id", sa.Text(), nullable=True),
        sa.Column("created_by_task_run_id", sa.String(length=36), nullable=False),
        sa.Column("created_by_task_step_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "input_tokens >= 0", name=op.f("ck_llm_artifacts_input_tokens_nonnegative")
        ),
        sa.CheckConstraint(
            "json_valid(output_json)", name=op.f("ck_llm_artifacts_output_json_valid")
        ),
        sa.CheckConstraint(
            "length(generation_config_hash) = 64",
            name=op.f("ck_llm_artifacts_generation_config_hash_length"),
        ),
        sa.CheckConstraint(
            "length(input_hash) = 64", name=op.f("ck_llm_artifacts_input_hash_length")
        ),
        sa.CheckConstraint(
            "length(output_hash) = 64", name=op.f("ck_llm_artifacts_output_hash_length")
        ),
        sa.CheckConstraint(
            "output_tokens >= 0", name=op.f("ck_llm_artifacts_output_tokens_nonnegative")
        ),
        sa.ForeignKeyConstraint(
            ["created_by_task_run_id", "created_by_task_step_id"],
            ["task_steps.task_run_id", "task_steps.id"],
            name="fk_llm_artifacts_created_by_task_step",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_task_run_id"],
            ["task_runs.id"],
            name=op.f("fk_llm_artifacts_created_by_task_run_id_task_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_artifacts")),
        sa.UniqueConstraint(
            "operation",
            "provider",
            "model",
            "prompt_version",
            "schema_version",
            "generation_config_hash",
            "input_hash",
            name="uq_llm_artifacts_cache_identity",
        ),
    )
    op.create_index("ix_llm_artifacts_created", "llm_artifacts", ["created_at"], unique=False)
    op.create_index("ix_llm_artifacts_output_hash", "llm_artifacts", ["output_hash"], unique=False)
    op.create_index(
        "ix_llm_artifacts_task_run", "llm_artifacts", ["created_by_task_run_id"], unique=False
    )
    op.create_index(
        "ix_llm_artifacts_task_step", "llm_artifacts", ["created_by_task_step_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_llm_artifacts_task_step", table_name="llm_artifacts")
    op.drop_index("ix_llm_artifacts_task_run", table_name="llm_artifacts")
    op.drop_index("ix_llm_artifacts_output_hash", table_name="llm_artifacts")
    op.drop_index("ix_llm_artifacts_created", table_name="llm_artifacts")
    op.drop_table("llm_artifacts")
    op.drop_index("ix_task_steps_status", table_name="task_steps")
    op.drop_index("ix_task_steps_run_order", table_name="task_steps")
    op.drop_index("ix_task_steps_error", table_name="task_steps")
    op.drop_table("task_steps")
    op.drop_index(
        "uq_task_runs_active_business",
        table_name="task_runs",
        sqlite_where=sa.text("status IN ('queued','running')"),
    )
    op.drop_index("ix_task_runs_type", table_name="task_runs")
    op.drop_index("ix_task_runs_created", table_name="task_runs")
    op.drop_index("ix_task_runs_status", table_name="task_runs")
    op.drop_index("ix_task_runs_parent", table_name="task_runs")
    op.drop_index("ix_task_runs_heartbeat", table_name="task_runs")
    op.drop_index("ix_task_runs_episode", table_name="task_runs")
    op.drop_table("task_runs")
    op.drop_index("ix_publications_type", table_name="publications")
    op.drop_index("ix_publications_published", table_name="publications")
    op.drop_index("ix_publications_status", table_name="publications")
    op.drop_index("ix_publications_remote", table_name="publications")
    op.drop_table("publications")
    op.drop_index("ix_episode_items_event", table_name="episode_items")
    op.drop_index("ix_episode_items_episode", table_name="episode_items")
    op.drop_table("episode_items")
    op.drop_index("ix_audio_segments_sha", table_name="audio_segments")
    op.drop_index("ix_audio_segments_episode_revision_status", table_name="audio_segments")
    op.drop_index("ix_audio_segments_cache", table_name="audio_segments")
    op.drop_table("audio_segments")
    op.drop_index("ix_sources_enabled_priority", table_name="sources")
    op.drop_index("ix_sources_kind", table_name="sources")
    op.drop_table("sources")
    op.drop_index("ix_news_events_importance", table_name="news_events")
    op.drop_index("ix_news_events_date", table_name="news_events")
    op.drop_index("ix_news_events_status", table_name="news_events")
    op.drop_index("ix_news_events_signature", table_name="news_events")
    op.drop_index("ix_news_events_representative", table_name="news_events")
    op.drop_table("news_events")
    op.drop_index("ix_episodes_published", table_name="episodes")
    op.drop_index("ix_episodes_date", table_name="episodes")
    op.drop_index("ix_episodes_status", table_name="episodes")
    op.drop_table("episodes")
    op.drop_index(
        "uq_articles_source_external",
        table_name="articles",
        sqlite_where=sa.text("external_id IS NOT NULL"),
    )
    op.drop_index("ix_articles_title_hash", table_name="articles")
    op.drop_index("ix_articles_published", table_name="articles")
    op.drop_index("ix_articles_status", table_name="articles")
    op.drop_index("ix_articles_source", table_name="articles")
    op.drop_index("ix_articles_event", table_name="articles")
    op.drop_index("ix_articles_duplicate", table_name="articles")
    op.drop_index("ix_articles_content_hash", table_name="articles")
    op.drop_table("articles")
