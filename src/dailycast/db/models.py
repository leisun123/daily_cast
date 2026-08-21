"""SQLAlchemy 2.x mappings for the complete DailyCast V1 relational schema."""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    and_,
    desc,
)
from sqlalchemy import (
    text as sql_text,
)
from sqlalchemy.orm import Mapped, foreign, mapped_column, relationship

from dailycast.db.base import Base


def utc_now() -> datetime:
    """Return the UTC timestamp written by the application for model timestamps."""
    return datetime.now(UTC)


def enum_column(enum_class: type[StrEnum]) -> Enum:
    """Store a StrEnum's documented values in SQLite with a CHECK constraint."""
    return Enum(
        enum_class,
        name=enum_class.__name__.lower(),
        native_enum=False,
        create_constraint=True,
        values_callable=lambda members: [member.value for member in members],
    )


class SourceKind(StrEnum):
    """Supported configured source kinds."""

    RSS = "rss"
    HTML_LIST = "html_list"
    WEB_RESEARCH = "web_research"


class ArticleStatus(StrEnum):
    """Article extraction and deterministic-filter states."""

    DISCOVERED = "discovered"
    FETCHING = "fetching"
    EXTRACTED = "extracted"
    ELIGIBLE = "eligible"
    FILTERED = "filtered"
    DUPLICATE = "duplicate"
    EXTRACTION_FAILED = "extraction_failed"


class NewsEventStatus(StrEnum):
    """Editorial status for a clustered news event."""

    CANDIDATE = "candidate"
    SCORED = "scored"
    SELECTED = "selected"
    REJECTED = "rejected"


class EpisodeStatus(StrEnum):
    """Documented V1 episode lifecycle states."""

    DRAFT = "draft"
    REVIEW_REQUIRED = "review_required"
    APPROVED = "approved"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"


class ScriptOrigin(StrEnum):
    """Origin of the currently stored episode script."""

    GENERATED = "generated"
    EDITED = "edited"


class TaskType(StrEnum):
    """Supported task requests."""

    DAILY_GENERATE = "daily_generate"
    PUBLISH = "publish"
    REGENERATE_EPISODE = "regenerate_episode"
    REGENERATE_SEGMENT = "regenerate_segment"


class TriggerType(StrEnum):
    """Task submission origins."""

    MANUAL = "manual"
    SCHEDULED = "scheduled"
    RETRY = "retry"


class TaskRunStatus(StrEnum):
    """TaskRun lifecycle states."""

    QUEUED = "queued"
    RUNNING = "running"
    WAITING_ACTION = "waiting_action"
    SUCCEEDED = "succeeded"
    SUCCEEDED_WITH_WARNINGS = "succeeded_with_warnings"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


class TaskStepStatus(StrEnum):
    """One task-step attempt's lifecycle states."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    SUCCEEDED_WITH_WARNINGS = "succeeded_with_warnings"
    FAILED = "failed"
    SKIPPED = "skipped"


class LLMOperation(StrEnum):
    """Structured LLM operations; briefing generation bypasses artifact caching."""

    SCORE_EVENTS = "score_events"
    GENERATE_OUTLINE = "generate_outline"
    GENERATE_SCRIPT = "generate_script"
    GENERATE_METADATA = "generate_metadata"
    REVIEW_SCRIPT = "review_script"
    GENERATE_BRIEFING = "generate_briefing"


class AudioSegmentStatus(StrEnum):
    """Per-segment synthesis states."""

    PENDING = "pending"
    SYNTHESIZING = "synthesizing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STALE = "stale"


class PublisherType(StrEnum):
    """V1 ships only the self-hosted RSS publisher target."""

    RSS = "rss"


class PublicationStatus(StrEnum):
    """V1 publication state without future RPA-only states."""

    PENDING = "pending"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"


class PublicationPlatform(StrEnum):
    """Configured distribution destinations for one generated episode."""

    RSS = "rss"
    NETEASE = "netease"
    XIAOYUZHOU = "xiaoyuzhou"


class PublicationTargetStatus(StrEnum):
    """Per-platform lifecycle without changing the Episode generation lifecycle."""

    PENDING = "pending"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    NEEDS_ATTENTION = "needs_attention"
    FAILED = "failed"


class Source(Base):
    """A configured RSS or HTML-list news source."""

    __tablename__ = "sources"
    __table_args__ = (
        CheckConstraint("enabled IN (0, 1)", name="enabled_boolean"),
        CheckConstraint("priority BETWEEN 0 AND 100", name="priority_range"),
        CheckConstraint(
            "request_timeout_seconds BETWEEN 1 AND 120", name="request_timeout_seconds_range"
        ),
        CheckConstraint("max_items_per_run BETWEEN 1 AND 500", name="max_items_per_run_range"),
        Index("ix_sources_enabled_priority", "enabled", desc("priority")),
        Index("ix_sources_kind", "kind"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[SourceKind] = mapped_column(enum_column(SourceKind), nullable=False)
    entry_url: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_entry_url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean(create_constraint=False), nullable=False, default=True, server_default=sql_text("1")
    )
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=50, server_default=sql_text("50")
    )
    language: Mapped[str | None] = mapped_column(Text)
    config_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", server_default=sql_text("'{}'")
    )
    request_timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=20, server_default=sql_text("20")
    )
    max_items_per_run: Mapped[int] = mapped_column(
        Integer, nullable=False, default=50, server_default=sql_text("50")
    )
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(Text)
    last_error_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

    articles: Mapped[list[Article]] = relationship(back_populates="source")


class Article(Base):
    """A discovered article plus its deterministic processing state."""

    __tablename__ = "articles"
    __table_args__ = (
        CheckConstraint("published_at_inferred IN (0, 1)", name="published_at_inferred_boolean"),
        CheckConstraint("json_valid(metadata_json)", name="metadata_json_valid"),
        Index(
            "uq_articles_source_external",
            "source_id",
            "external_id",
            unique=True,
            sqlite_where=sql_text("external_id IS NOT NULL"),
        ),
        Index("ix_articles_source", "source_id"),
        Index("ix_articles_published", desc("published_at")),
        Index("ix_articles_status", "status"),
        Index("ix_articles_title_hash", "title_hash"),
        Index("ix_articles_content_hash", "content_hash"),
        Index("ix_articles_event", "news_event_id"),
        Index("ix_articles_duplicate", "duplicate_of_article_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(
        ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False
    )
    external_id: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_url: Mapped[str] = mapped_column(Text, nullable=False)
    url_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_title: Mapped[str] = mapped_column(Text, nullable=False)
    title_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    content_text: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    simhash: Mapped[str | None] = mapped_column(String(16))
    language: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at_inferred: Mapped[bool] = mapped_column(
        Boolean(create_constraint=False),
        nullable=False,
        default=False,
        server_default=sql_text("0"),
    )
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    http_status: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[ArticleStatus] = mapped_column(enum_column(ArticleStatus), nullable=False)
    filter_reason: Mapped[str | None] = mapped_column(Text)
    duplicate_of_article_id: Mapped[int | None] = mapped_column(
        ForeignKey("articles.id", ondelete="SET NULL")
    )
    news_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("news_events.id", ondelete="SET NULL")
    )
    error_code: Mapped[str | None] = mapped_column(Text)
    error_summary: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", server_default=sql_text("'{}'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

    source: Mapped[Source] = relationship(back_populates="articles")
    duplicate_of: Mapped[Article | None] = relationship(
        "Article",
        remote_side="Article.id",
        foreign_keys=[duplicate_of_article_id],
        back_populates="duplicates",
    )
    duplicates: Mapped[list[Article]] = relationship(
        "Article", foreign_keys=[duplicate_of_article_id], back_populates="duplicate_of"
    )
    news_event: Mapped[NewsEvent | None] = relationship(
        "NewsEvent", foreign_keys=[news_event_id], back_populates="articles"
    )
    representative_for: Mapped[list[NewsEvent]] = relationship(
        "NewsEvent",
        foreign_keys="NewsEvent.representative_article_id",
        back_populates="representative_article",
    )


class NewsEvent(Base):
    """A clustered event represented by one persisted article."""

    __tablename__ = "news_events"
    __table_args__ = (
        CheckConstraint("article_count >= 1", name="article_count_positive"),
        CheckConstraint("source_count >= 1", name="source_count_positive"),
        CheckConstraint("json_valid(risk_flags_json)", name="risk_flags_json_valid"),
        CheckConstraint("score_json IS NULL OR json_valid(score_json)", name="score_json_valid"),
        CheckConstraint(
            "importance_score IS NULL OR importance_score BETWEEN 0 AND 100",
            name="importance_score_range",
        ),
        CheckConstraint(
            "relevance_score IS NULL OR relevance_score BETWEEN 0 AND 100",
            name="relevance_score_range",
        ),
        CheckConstraint(
            "confidence_score IS NULL OR confidence_score BETWEEN 0 AND 100",
            name="confidence_score_range",
        ),
        Index("ix_news_events_date", desc("event_date")),
        Index("ix_news_events_status", "status"),
        Index("ix_news_events_importance", desc("importance_score")),
        Index("ix_news_events_signature", "cluster_signature"),
        Index("ix_news_events_representative", "representative_article_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    representative_article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="RESTRICT"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    status: Mapped[NewsEventStatus] = mapped_column(enum_column(NewsEventStatus), nullable=False)
    first_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    article_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=sql_text("1")
    )
    source_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=sql_text("1")
    )
    deterministic_score: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, server_default=sql_text("0")
    )
    importance_score: Mapped[float | None] = mapped_column(Float)
    relevance_score: Mapped[float | None] = mapped_column(Float)
    confidence_score: Mapped[float | None] = mapped_column(Float)
    selection_reason: Mapped[str | None] = mapped_column(Text)
    risk_flags_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default=sql_text("'[]'")
    )
    score_json: Mapped[str | None] = mapped_column(Text)
    cluster_algorithm: Mapped[str] = mapped_column(Text, nullable=False, default="tfidf_char")
    cluster_version: Mapped[str] = mapped_column(Text, nullable=False)
    cluster_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.58)
    cluster_signature: Mapped[str] = mapped_column(String(64), nullable=False)
    llm_model: Mapped[str | None] = mapped_column(Text)
    llm_prompt_version: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

    representative_article: Mapped[Article] = relationship(
        "Article", foreign_keys=[representative_article_id], back_populates="representative_for"
    )
    articles: Mapped[list[Article]] = relationship(
        "Article", foreign_keys="Article.news_event_id", back_populates="news_event"
    )
    episode_items: Mapped[list[EpisodeItem]] = relationship(back_populates="news_event")


class Episode(Base):
    """A date-and-edition-specific generated podcast episode."""

    __tablename__ = "episodes"
    __table_args__ = (
        CheckConstraint(
            "outline_json IS NULL OR json_valid(outline_json)", name="outline_json_valid"
        ),
        CheckConstraint("script_json IS NULL OR json_valid(script_json)", name="script_json_valid"),
        CheckConstraint("review_json IS NULL OR json_valid(review_json)", name="review_json_valid"),
        CheckConstraint("news_count >= 0", name="news_count_nonnegative"),
        CheckConstraint(
            "generation_time_seconds IS NULL OR generation_time_seconds >= 0",
            name="generation_time_seconds_nonnegative",
        ),
        UniqueConstraint("episode_date", "edition", name="uq_episodes_date_edition"),
        Index("ix_episodes_status", "status"),
        Index("ix_episodes_date", desc("episode_date")),
        Index("ix_episodes_published", desc("published_at")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    episode_date: Mapped[date] = mapped_column(Date, nullable=False)
    edition: Mapped[str] = mapped_column(
        Text, nullable=False, default="daily", server_default=sql_text("'daily'")
    )
    status: Mapped[EpisodeStatus] = mapped_column(
        enum_column(EpisodeStatus),
        nullable=False,
        default=EpisodeStatus.DRAFT,
        server_default=sql_text("'draft'"),
    )
    lock_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=sql_text("1")
    )
    title: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    outline_json: Mapped[str | None] = mapped_column(Text)
    script_json: Mapped[str | None] = mapped_column(Text)
    script_text: Mapped[str | None] = mapped_column(Text)
    script_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sql_text("0")
    )
    script_hash: Mapped[str | None] = mapped_column(String(64))
    script_origin: Mapped[ScriptOrigin | None] = mapped_column(enum_column(ScriptOrigin))
    review_json: Mapped[str | None] = mapped_column(Text)
    target_duration_seconds: Mapped[int | None] = mapped_column(Integer)
    actual_duration_ms: Mapped[int | None] = mapped_column(Integer)
    news_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sql_text("0")
    )
    generation_time_seconds: Mapped[int | None] = mapped_column(Integer)
    audio_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sql_text("0")
    )
    audio_manifest_hash: Mapped[str | None] = mapped_column(String(64))
    draft_audio_path: Mapped[str | None] = mapped_column(Text)
    draft_audio_sha256: Mapped[str | None] = mapped_column(String(64))
    approved_script_revision: Mapped[int | None] = mapped_column(Integer)
    approved_audio_version: Mapped[int | None] = mapped_column(Integer)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(Text)
    error_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

    episode_items: Mapped[list[EpisodeItem]] = relationship(
        back_populates="episode", cascade="all, delete-orphan"
    )
    audio_segments: Mapped[list[AudioSegment]] = relationship(
        back_populates="episode", cascade="all, delete-orphan"
    )
    task_runs: Mapped[list[TaskRun]] = relationship(back_populates="episode")
    publications: Mapped[list[Publication]] = relationship(back_populates="episode")
    publication_targets: Mapped[list[PublicationTarget]] = relationship(
        back_populates="episode", cascade="all, delete-orphan"
    )


class EpisodeItem(Base):
    """An immutable editorial snapshot of one selected event in an episode."""

    __tablename__ = "episode_items"
    __table_args__ = (
        CheckConstraint("position >= 1", name="position_positive"),
        CheckConstraint("json_valid(score_snapshot_json)", name="score_snapshot_json_valid"),
        CheckConstraint(
            "json_valid(source_article_ids_json)", name="source_article_ids_json_valid"
        ),
        UniqueConstraint("episode_id", "news_event_id", name="uq_episode_items_episode_event"),
        UniqueConstraint("episode_id", "position", name="uq_episode_items_episode_position"),
        Index("ix_episode_items_event", "news_event_id"),
        Index("ix_episode_items_episode", "episode_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    episode_id: Mapped[int] = mapped_column(
        ForeignKey("episodes.id", ondelete="CASCADE"), nullable=False
    )
    news_event_id: Mapped[int] = mapped_column(
        ForeignKey("news_events.id", ondelete="RESTRICT"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    event_title_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    selection_reason_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    score_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    source_article_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    section_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

    episode: Mapped[Episode] = relationship(back_populates="episode_items")
    news_event: Mapped[NewsEvent] = relationship(back_populates="episode_items")


class TaskRun(Base):
    """A durable task request, status, and resource-use audit record."""

    __tablename__ = "task_runs"
    __table_args__ = (
        CheckConstraint("json_valid(config_snapshot_json)", name="config_snapshot_json_valid"),
        CheckConstraint("json_valid(request_json)", name="request_json_valid"),
        CheckConstraint("retryable IN (0, 1)", name="retryable_boolean"),
        CheckConstraint("cache_hit_count >= 0", name="cache_hit_count_nonnegative"),
        Index(
            "uq_task_runs_active_business",
            "business_key",
            unique=True,
            sqlite_where=sql_text("status IN ('queued','running')"),
        ),
        Index("ix_task_runs_created", desc("created_at")),
        Index("ix_task_runs_status", "status"),
        Index("ix_task_runs_type", "task_type"),
        Index("ix_task_runs_episode", "episode_id"),
        Index("ix_task_runs_parent", "parent_task_run_id"),
        Index("ix_task_runs_heartbeat", "heartbeat_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_type: Mapped[TaskType] = mapped_column(enum_column(TaskType), nullable=False)
    business_key: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    trigger_type: Mapped[TriggerType] = mapped_column(enum_column(TriggerType), nullable=False)
    status: Mapped[TaskRunStatus] = mapped_column(enum_column(TaskRunStatus), nullable=False)
    current_step: Mapped[str | None] = mapped_column(Text)
    episode_id: Mapped[int | None] = mapped_column(ForeignKey("episodes.id", ondelete="SET NULL"))
    parent_task_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("task_runs.id", ondelete="SET NULL")
    )
    pipeline_version: Mapped[str] = mapped_column(Text, nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    config_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    request_json: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    warning_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sql_text("0")
    )
    llm_call_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sql_text("0")
    )
    llm_input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sql_text("0")
    )
    llm_output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sql_text("0")
    )
    tts_character_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sql_text("0")
    )
    cache_hit_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sql_text("0")
    )
    retryable: Mapped[bool] = mapped_column(
        Boolean(create_constraint=False),
        nullable=False,
        default=False,
        server_default=sql_text("0"),
    )
    error_code: Mapped[str | None] = mapped_column(Text)
    error_summary: Mapped[str | None] = mapped_column(Text)
    log_path: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

    episode: Mapped[Episode | None] = relationship(back_populates="task_runs")
    parent_task_run: Mapped[TaskRun | None] = relationship(
        "TaskRun", remote_side="TaskRun.id", back_populates="resumed_task_runs"
    )
    resumed_task_runs: Mapped[list[TaskRun]] = relationship(
        "TaskRun", back_populates="parent_task_run"
    )
    steps: Mapped[list[TaskStep]] = relationship(
        back_populates="task_run", cascade="all, delete-orphan"
    )
    llm_artifacts: Mapped[list[LLMArtifact]] = relationship(
        back_populates="created_by_task_run", foreign_keys="LLMArtifact.created_by_task_run_id"
    )


class TaskStep(Base):
    """One attempted logical step within a TaskRun."""

    __tablename__ = "task_steps"
    __table_args__ = (
        CheckConstraint("attempt >= 1", name="attempt_positive"),
        CheckConstraint(
            "checkpoint_json IS NULL OR json_valid(checkpoint_json)", name="checkpoint_json_valid"
        ),
        CheckConstraint("json_valid(details_json)", name="details_json_valid"),
        CheckConstraint("tts_character_count >= 0", name="tts_character_count_nonnegative"),
        CheckConstraint("retryable IN (0, 1)", name="retryable_boolean"),
        UniqueConstraint(
            "task_run_id", "step_name", "attempt", name="uq_task_steps_run_name_attempt"
        ),
        UniqueConstraint("task_run_id", "id", name="uq_task_steps_run_id"),
        Index("ix_task_steps_run_order", "task_run_id", "step_order"),
        Index("ix_task_steps_status", "status"),
        Index("ix_task_steps_error", "error_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_run_id: Mapped[str] = mapped_column(
        ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=False
    )
    step_name: Mapped[str] = mapped_column(Text, nullable=False)
    step_order: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[TaskStepStatus] = mapped_column(enum_column(TaskStepStatus), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    input_count: Mapped[int | None] = mapped_column(Integer)
    output_count: Mapped[int | None] = mapped_column(Integer)
    warning_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sql_text("0")
    )
    input_fingerprint: Mapped[str | None] = mapped_column(String(64))
    output_fingerprint: Mapped[str | None] = mapped_column(String(64))
    checkpoint_json: Mapped[str | None] = mapped_column(Text)
    details_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", server_default=sql_text("'{}'")
    )
    artifact_path: Mapped[str | None] = mapped_column(Text)
    llm_call_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sql_text("0")
    )
    llm_input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sql_text("0")
    )
    llm_output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sql_text("0")
    )
    tts_character_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sql_text("0")
    )
    retryable: Mapped[bool] = mapped_column(
        Boolean(create_constraint=False),
        nullable=False,
        default=False,
        server_default=sql_text("0"),
    )
    error_code: Mapped[str | None] = mapped_column(Text)
    error_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

    task_run: Mapped[TaskRun] = relationship(back_populates="steps")
    llm_artifacts: Mapped[list[LLMArtifact]] = relationship(
        "LLMArtifact",
        primaryjoin=lambda: and_(
            TaskStep.task_run_id == foreign(LLMArtifact.created_by_task_run_id),
            TaskStep.id == foreign(LLMArtifact.created_by_task_step_id),
        ),
        viewonly=True,
    )


class LLMArtifact(Base):
    """A validated, immutable structured LLM result reusable by exact cache identity."""

    __tablename__ = "llm_artifacts"
    __table_args__ = (
        CheckConstraint(
            "length(generation_config_hash) = 64", name="generation_config_hash_length"
        ),
        CheckConstraint("length(input_hash) = 64", name="input_hash_length"),
        CheckConstraint("json_valid(output_json)", name="output_json_valid"),
        CheckConstraint("length(output_hash) = 64", name="output_hash_length"),
        CheckConstraint("input_tokens >= 0", name="input_tokens_nonnegative"),
        CheckConstraint("output_tokens >= 0", name="output_tokens_nonnegative"),
        UniqueConstraint(
            "operation",
            "provider",
            "model",
            "prompt_version",
            "schema_version",
            "generation_config_hash",
            "input_hash",
            name="uq_llm_artifacts_cache_identity",
        ),
        ForeignKeyConstraint(
            ["created_by_task_run_id", "created_by_task_step_id"],
            ["task_steps.task_run_id", "task_steps.id"],
            ondelete="RESTRICT",
            name="fk_llm_artifacts_created_by_task_step",
        ),
        Index("ix_llm_artifacts_created", "created_at"),
        Index("ix_llm_artifacts_task_run", "created_by_task_run_id"),
        Index("ix_llm_artifacts_task_step", "created_by_task_step_id"),
        Index("ix_llm_artifacts_output_hash", "output_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operation: Mapped[LLMOperation] = mapped_column(enum_column(LLMOperation), nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    generation_config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    output_json: Mapped[str] = mapped_column(Text, nullable=False)
    output_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sql_text("0")
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sql_text("0")
    )
    provider_request_id: Mapped[str | None] = mapped_column(Text)
    created_by_task_run_id: Mapped[str] = mapped_column(
        ForeignKey("task_runs.id", ondelete="RESTRICT"), nullable=False
    )
    created_by_task_step_id: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )

    created_by_task_run: Mapped[TaskRun] = relationship(
        back_populates="llm_artifacts", foreign_keys=[created_by_task_run_id]
    )
    created_by_task_step: Mapped[TaskStep] = relationship(
        "TaskStep",
        primaryjoin=lambda: and_(
            foreign(LLMArtifact.created_by_task_run_id) == TaskStep.task_run_id,
            foreign(LLMArtifact.created_by_task_step_id) == TaskStep.id,
        ),
        viewonly=True,
    )


class AudioSegment(Base):
    """A versioned TTS segment and its complete audio cache identity."""

    __tablename__ = "audio_segments"
    __table_args__ = (
        CheckConstraint("segment_index >= 0", name="segment_index_nonnegative"),
        CheckConstraint("length(cache_key) = 64", name="cache_key_length"),
        CheckConstraint("length(provider_config_hash) = 64", name="provider_config_hash_length"),
        CheckConstraint("length(tts_preprocess_hash) = 64", name="tts_preprocess_hash_length"),
        UniqueConstraint(
            "episode_id",
            "script_revision",
            "segment_index",
            name="uq_audio_segments_episode_revision_index",
        ),
        Index(
            "ix_audio_segments_cache",
            "cache_key",
            "provider_config_hash",
            "tts_preprocess_hash",
            "status",
        ),
        Index(
            "ix_audio_segments_episode_revision_status", "episode_id", "script_revision", "status"
        ),
        Index("ix_audio_segments_sha", "sha256"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    episode_id: Mapped[int] = mapped_column(
        ForeignKey("episodes.id", ondelete="CASCADE"), nullable=False
    )
    script_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    segment_index: Mapped[int] = mapped_column(Integer, nullable=False)
    segmenter_version: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    cache_key: Mapped[str] = mapped_column(String(64), nullable=False)
    force_nonce: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    voice: Mapped[str] = mapped_column(Text, nullable=False)
    speed: Mapped[float] = mapped_column(
        Float, nullable=False, default=1.0, server_default=sql_text("1.0")
    )
    format: Mapped[str] = mapped_column(
        Text, nullable=False, default="mp3", server_default=sql_text("'mp3'")
    )
    provider_config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    tts_preprocess_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[AudioSegmentStatus] = mapped_column(
        enum_column(AudioSegmentStatus), nullable=False
    )
    audio_path: Mapped[str | None] = mapped_column(Text)
    mime_type: Mapped[str | None] = mapped_column(Text)
    byte_size: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sql_text("0")
    )
    provider_request_id: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    error_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

    episode: Mapped[Episode] = relationship(back_populates="audio_segments")


class Publication(Base):
    """A single idempotent V1 RSS publication target for one episode."""

    __tablename__ = "publications"
    __table_args__ = (
        CheckConstraint(
            "response_summary_json IS NULL OR json_valid(response_summary_json)",
            name="response_summary_json_valid",
        ),
        UniqueConstraint(
            "episode_id",
            "publisher_type",
            "target_key",
            name="uq_publications_episode_publisher_target",
        ),
        Index("ix_publications_status", "status"),
        Index("ix_publications_type", "publisher_type"),
        Index("ix_publications_remote", "remote_id"),
        Index("ix_publications_published", desc("published_at")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    episode_id: Mapped[int] = mapped_column(
        ForeignKey("episodes.id", ondelete="RESTRICT"), nullable=False
    )
    publisher_type: Mapped[PublisherType] = mapped_column(
        enum_column(PublisherType), nullable=False
    )
    target_key: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[PublicationStatus] = mapped_column(
        enum_column(PublicationStatus), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sql_text("0")
    )
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    remote_id: Mapped[str | None] = mapped_column(Text)
    remote_url: Mapped[str | None] = mapped_column(Text)
    public_asset_path: Mapped[str | None] = mapped_column(Text)
    public_audio_url: Mapped[str | None] = mapped_column(Text)
    asset_sha256: Mapped[str | None] = mapped_column(String(64))
    asset_byte_size: Mapped[int | None] = mapped_column(Integer)
    feed_guid: Mapped[str | None] = mapped_column(Text)
    response_summary_json: Mapped[str | None] = mapped_column(Text)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(Text)
    error_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

    episode: Mapped[Episode] = relationship(back_populates="publications")


class PublicationTarget(Base):
    """One independent external distribution state for an Episode and platform."""

    __tablename__ = "publication_targets"
    __table_args__ = (
        CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        UniqueConstraint("episode_id", "platform", name="uq_publication_targets_episode_platform"),
        Index("ix_publication_targets_status", "status"),
        Index("ix_publication_targets_platform", "platform"),
        Index("ix_publication_targets_remote", "remote_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    episode_id: Mapped[int] = mapped_column(
        ForeignKey("episodes.id", ondelete="CASCADE"), nullable=False
    )
    platform: Mapped[PublicationPlatform] = mapped_column(
        enum_column(PublicationPlatform), nullable=False
    )
    status: Mapped[PublicationTargetStatus] = mapped_column(
        enum_column(PublicationTargetStatus), nullable=False
    )
    remote_id: Mapped[str | None] = mapped_column(Text)
    remote_url: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sql_text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

    episode: Mapped[Episode] = relationship(back_populates="publication_targets")
