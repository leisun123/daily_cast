"""Focused SQLAlchemy repositories for DailyCast V1 persistence operations."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.orm import Session

from dailycast.db.models import (
    Article,
    AudioSegment,
    AudioSegmentStatus,
    Episode,
    EpisodeItem,
    EpisodeStatus,
    LLMArtifact,
    NewsEvent,
    Publication,
    PublicationStatus,
    Source,
    TaskRun,
    TaskRunStatus,
    TaskStep,
    TaskStepStatus,
    utc_now,
)


def _apply_changes(instance: Any, changes: dict[str, Any]) -> None:
    """Apply explicitly supplied mapped values to one pending ORM instance."""
    for name, value in changes.items():
        setattr(instance, name, value)


class SourceRepository:
    """Persistence operations for configured source rows."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, **values: Any) -> Source:
        """Create and flush a source without committing the surrounding transaction."""
        source = Source(**values)
        self._session.add(source)
        self._session.flush()
        return source

    def get(self, source_id: str) -> Source | None:
        """Return a source by its stable configuration slug."""
        return self._session.get(Source, source_id)

    def list(self) -> list[Source]:
        """List sources in deterministic priority and identifier order."""
        statement = select(Source).order_by(Source.priority.desc(), Source.id)
        return list(self._session.scalars(statement))

    def update(self, source: Source, **changes: Any) -> Source:
        """Update source metadata or configuration while retaining its identity."""
        _apply_changes(source, changes)
        source.updated_at = utc_now()
        self._session.flush()
        return source

    def disable(self, source: Source) -> Source:
        """Soft-disable a source while preserving its Article history."""
        return self.update(source, enabled=False)


class ArticleRepository:
    """Persistence operations for URL-identity article rows."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert(self, **values: Any) -> Article:
        """Insert an article or update its latest metadata by unique URL hash."""
        url_hash = values["url_hash"]
        article = self.get_by_url_hash(url_hash)
        if article is None:
            article = Article(**values)
            self._session.add(article)
            self._session.flush()
            return article

        discovered_at = values.pop("discovered_at", None)
        for name, value in values.items():
            if name not in {"id", "url_hash", "created_at"}:
                setattr(article, name, value)
        if discovered_at is not None and discovered_at < article.discovered_at:
            article.discovered_at = discovered_at
        article.updated_at = utc_now()
        self._session.flush()
        return article

    def get_by_url_hash(self, url_hash: str) -> Article | None:
        """Return the unique article identified by its normalized URL hash."""
        return self._session.scalar(select(Article).where(Article.url_hash == url_hash))

    def get(self, article_id: int) -> Article | None:
        """Return one Article by its durable integer identifier."""
        return self._session.get(Article, article_id)

    def list_by_ids(self, article_ids: tuple[int, ...]) -> list[Article]:
        """Return a deterministic subset for a pipeline checkpoint without scanning history."""
        if not article_ids:
            return []
        statement = select(Article).where(Article.id.in_(article_ids)).order_by(Article.id)
        return list(self._session.scalars(statement))

    def update(self, article: Article, **changes: Any) -> Article:
        """Persist an Article status or extraction update inside the caller-owned transaction."""
        _apply_changes(article, changes)
        article.updated_at = utc_now()
        self._session.flush()
        return article

    def list(self) -> list[Article]:
        """List articles from newest discovery to oldest."""
        statement = select(Article).order_by(Article.discovered_at.desc(), Article.id.desc())
        return list(self._session.scalars(statement))


class NewsEventRepository:
    """Persistence operations for clustered news-event rows."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, **values: Any) -> NewsEvent:
        """Create and flush a NewsEvent row."""
        event = NewsEvent(**values)
        self._session.add(event)
        self._session.flush()
        return event

    def get(self, event_id: int) -> NewsEvent | None:
        """Return one event by internal identifier."""
        return self._session.get(NewsEvent, event_id)

    def get_by_event_key(self, event_key: str) -> NewsEvent | None:
        """Return the stable deterministic event identity used for idempotent reclustering."""
        return self._session.scalar(select(NewsEvent).where(NewsEvent.event_key == event_key))

    def update(self, event: NewsEvent, **changes: Any) -> NewsEvent:
        """Refresh an unselected candidate event from a deterministic clustering replay."""
        _apply_changes(event, changes)
        event.updated_at = utc_now()
        self._session.flush()
        return event


class EpisodeRepository:
    """Persistence operations for episode rows."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, **values: Any) -> Episode:
        """Create and flush an episode in its initial draft state."""
        episode = Episode(**values)
        self._session.add(episode)
        self._session.flush()
        return episode

    def get(self, episode_id: int) -> Episode | None:
        """Return an episode by internal identifier."""
        return self._session.get(Episode, episode_id)

    def get_by_date_and_edition(self, episode_date: date, edition: str) -> Episode | None:
        """Return the one idempotent Episode identity for a business date and edition."""
        statement = select(Episode).where(
            Episode.episode_date == episode_date,
            Episode.edition == edition,
        )
        return self._session.scalar(statement)

    def list(self) -> list[Episode]:
        """List Episodes from newest business date to oldest in deterministic edition order."""
        statement = select(Episode).order_by(
            Episode.episode_date.desc(), Episode.edition, Episode.id
        )
        return list(self._session.scalars(statement))

    def update_status(self, episode: Episode, status: EpisodeStatus | str) -> Episode:
        """Persist an explicit state change; lifecycle validation belongs to later services."""
        episode.status = status  # type: ignore[assignment]
        episode.updated_at = utc_now()
        self._session.flush()
        return episode

    def increment_lock_version(self, episode: Episode) -> Episode:
        """Advance the explicit optimistic-lock token after an Episode mutation."""
        episode.lock_version += 1
        episode.updated_at = utc_now()
        self._session.flush()
        return episode


class EpisodeItemRepository:
    """Persistence operations for immutable Episode-to-NewsEvent editorial snapshots."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, **values: Any) -> EpisodeItem:
        """Create and flush one immutable selected-event snapshot."""
        item = EpisodeItem(**values)
        self._session.add(item)
        self._session.flush()
        return item

    def list_by_episode(self, episode_id: int) -> list[EpisodeItem]:
        """Return one Episode snapshots in persisted playback order."""
        statement = (
            select(EpisodeItem)
            .where(EpisodeItem.episode_id == episode_id)
            .order_by(EpisodeItem.position, EpisodeItem.id)
        )
        return list(self._session.scalars(statement))


class TaskRunRepository:
    """Persistence operations for task submission and heartbeat records."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, **values: Any) -> TaskRun:
        """Create and flush a task run inside the caller's short transaction."""
        task_run = TaskRun(**values)
        self._session.add(task_run)
        self._session.flush()
        return task_run

    def get(self, task_run_id: str) -> TaskRun | None:
        """Return a task run by UUID text identifier."""
        return self._session.get(TaskRun, task_run_id)

    def get_by_idempotency_key(self, idempotency_key: str) -> TaskRun | None:
        """Return the one task request bound to a client idempotency key."""
        return self._session.scalar(
            select(TaskRun).where(TaskRun.idempotency_key == idempotency_key)
        )

    def list_queued(self) -> list[TaskRun]:
        """List durable queued tasks in submission order for startup recovery."""
        statement = (
            select(TaskRun)
            .where(TaskRun.status == TaskRunStatus.QUEUED)
            .order_by(TaskRun.created_at, TaskRun.id)
        )
        return list(self._session.scalars(statement))

    def list_stale_running(self, stale_before: datetime) -> list[TaskRun]:
        """List running tasks whose heartbeat is expired or was never written."""
        statement = (
            select(TaskRun)
            .where(
                TaskRun.status == TaskRunStatus.RUNNING,
                or_(
                    TaskRun.heartbeat_at < stale_before,
                    and_(TaskRun.heartbeat_at.is_(None), TaskRun.started_at < stale_before),
                ),
            )
            .order_by(TaskRun.started_at, TaskRun.id)
        )
        return list(self._session.scalars(statement))

    def get_active_by_business_key(self, business_key: str) -> TaskRun | None:
        """Return the unique queued or running task for the logical business key."""
        statement = select(TaskRun).where(
            TaskRun.business_key == business_key,
            TaskRun.status.in_((TaskRunStatus.QUEUED, TaskRunStatus.RUNNING)),
        )
        return self._session.scalar(statement)

    def update_status(
        self, task_run: TaskRun, status: TaskRunStatus | str, **changes: Any
    ) -> TaskRun:
        """Update task status and related audit fields without committing the transaction."""
        _apply_changes(task_run, changes)
        task_run.status = status  # type: ignore[assignment]
        task_run.updated_at = utc_now()
        self._session.flush()
        return task_run

    def update_current_step(self, task_run: TaskRun, current_step: str | None) -> TaskRun:
        """Record the latest checkpoint name while preserving the current task status."""
        task_run.current_step = current_step
        task_run.updated_at = utc_now()
        self._session.flush()
        return task_run

    def update_heartbeat(self, task_run: TaskRun, heartbeat_at: datetime) -> TaskRun:
        """Write the worker heartbeat through a caller-owned short transaction."""
        task_run.heartbeat_at = heartbeat_at
        task_run.updated_at = utc_now()
        self._session.flush()
        return task_run


class TaskStepRepository:
    """Persistence operations for one TaskRun's attempted step rows."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, **values: Any) -> TaskStep:
        """Create and flush a step attempt."""
        step = TaskStep(**values)
        self._session.add(step)
        self._session.flush()
        return step

    def get(self, step_id: int) -> TaskStep | None:
        """Return a TaskStep by its local identifier."""
        return self._session.get(TaskStep, step_id)

    def finish(
        self,
        step: TaskStep,
        *,
        status: TaskStepStatus | str,
        ended_at: datetime | None = None,
        **changes: Any,
    ) -> TaskStep:
        """Record final step values without rewriting a historical attempt later."""
        _apply_changes(step, changes)
        step.status = status  # type: ignore[assignment]
        step.ended_at = ended_at or utc_now()
        step.updated_at = utc_now()
        self._session.flush()
        return step


class LLMArtifactRepository:
    """Read-only-by-identity and immutable-insert access to validated LLM artifacts."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_cache_identity(
        self,
        *,
        operation: str,
        provider: str,
        model: str,
        prompt_version: str,
        schema_version: str,
        generation_config_hash: str,
        input_hash: str,
    ) -> LLMArtifact | None:
        """Return only the exact seven-part, previously validated cache identity."""
        statement = select(LLMArtifact).where(
            LLMArtifact.operation == operation,
            LLMArtifact.provider == provider,
            LLMArtifact.model == model,
            LLMArtifact.prompt_version == prompt_version,
            LLMArtifact.schema_version == schema_version,
            LLMArtifact.generation_config_hash == generation_config_hash,
            LLMArtifact.input_hash == input_hash,
        )
        return self._session.scalar(statement)

    def insert_validated(self, **values: Any) -> LLMArtifact:
        """Insert only a successful output already validated by the caller's local schema."""
        output_json = values["output_json"]
        try:
            json.loads(output_json)
        except (TypeError, json.JSONDecodeError) as error:
            msg = "output_json must be syntactically valid JSON after schema validation"
            raise ValueError(msg) from error
        artifact = LLMArtifact(**values)
        self._session.add(artifact)
        self._session.flush()
        return artifact

    def prune_before(self, cutoff: datetime, batch_size: int = 500) -> int:
        """Delete at most one retention batch of immutable, expired cache records."""
        artifact_id_statement = (
            select(LLMArtifact.id)
            .where(LLMArtifact.created_at < cutoff)
            .order_by(LLMArtifact.created_at, LLMArtifact.id)
            .limit(batch_size)
        )
        artifact_ids = list(self._session.scalars(artifact_id_statement))
        self._session.execute(delete(LLMArtifact).where(LLMArtifact.id.in_(artifact_ids)))
        return len(artifact_ids)


class AudioSegmentRepository:
    """Persistence operations for versioned TTS segment cache records."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_cache_key(self, cache_key: str, provider_config_hash: str) -> AudioSegment | None:
        """Return a reusable succeeded segment only for the complete cache identity."""
        statement = select(AudioSegment).where(
            AudioSegment.cache_key == cache_key,
            AudioSegment.provider_config_hash == provider_config_hash,
            AudioSegment.status == AudioSegmentStatus.SUCCEEDED,
        )
        return self._session.scalar(statement)

    def get_by_episode_revision_index(
        self, episode_id: int, script_revision: int, segment_index: int
    ) -> AudioSegment | None:
        """Return the one segment position for an Episode script revision."""
        statement = select(AudioSegment).where(
            AudioSegment.episode_id == episode_id,
            AudioSegment.script_revision == script_revision,
            AudioSegment.segment_index == segment_index,
        )
        return self._session.scalar(statement)

    def list_by_episode_revision(
        self, episode_id: int, *, script_revision: int
    ) -> list[AudioSegment]:
        """List one revision's segments in their deterministic playback order."""
        statement = (
            select(AudioSegment)
            .where(
                AudioSegment.episode_id == episode_id,
                AudioSegment.script_revision == script_revision,
            )
            .order_by(AudioSegment.segment_index, AudioSegment.id)
        )
        return list(self._session.scalars(statement))

    def create(self, **values: Any) -> AudioSegment:
        """Create and flush one episode-revision segment row."""
        segment = AudioSegment(**values)
        self._session.add(segment)
        self._session.flush()
        return segment

    def update(self, segment: AudioSegment, **changes: Any) -> AudioSegment:
        """Persist a segment lifecycle or cache-file update in the caller transaction."""
        _apply_changes(segment, changes)
        segment.updated_at = utc_now()
        self._session.flush()
        return segment


class PublicationRepository:
    """Persistence operations for idempotent V1 RSS publication rows."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, **values: Any) -> Publication:
        """Create and flush the one allowed publication row for a target."""
        publication = Publication(**values)
        self._session.add(publication)
        self._session.flush()
        return publication

    def get(self, publication_id: int) -> Publication | None:
        """Return one Publication by its durable database identifier."""
        return self._session.get(Publication, publication_id)

    def get_by_target(
        self, episode_id: int, publisher_type: str, target_key: str
    ) -> Publication | None:
        """Return the unique publication row for an episode and target."""
        statement = select(Publication).where(
            Publication.episode_id == episode_id,
            Publication.publisher_type == publisher_type,
            Publication.target_key == target_key,
        )
        return self._session.scalar(statement)

    def list_by_status(self, *statuses: PublicationStatus) -> list[Publication]:
        """List publication work in deterministic oldest-first order for reconciliation."""
        if not statuses:
            return []
        statement = (
            select(Publication)
            .where(Publication.status.in_(statuses))
            .order_by(Publication.created_at, Publication.id)
        )
        return list(self._session.scalars(statement))

    def list_published(self, *, target_key: str) -> list[Publication]:
        """Return stable Feed members.

        The publication service explicitly injects the current publishing candidate.
        """
        statement = (
            select(Publication)
            .where(
                Publication.status == PublicationStatus.PUBLISHED,
                Publication.target_key == target_key,
            )
            .order_by(Publication.published_at.desc(), Publication.id.desc())
        )
        return list(self._session.scalars(statement))

    def get_published_by_asset(
        self, *, episode_public_id: str, asset_filename: str
    ) -> Publication | None:
        """Resolve only one published immutable public asset for the anonymous media endpoint."""
        expected_path = f"media/episodes/{episode_public_id}/{asset_filename}"
        statement = (
            select(Publication)
            .join(Episode, Publication.episode_id == Episode.id)
            .where(
                Publication.status == PublicationStatus.PUBLISHED,
                Episode.public_id == episode_public_id,
                Publication.public_asset_path == expected_path,
            )
        )
        return self._session.scalar(statement)

    def update(self, publication: Publication, **changes: Any) -> Publication:
        """Persist one lifecycle, verification, or response-summary update.

        The surrounding UnitOfWork owns the transaction.
        """
        _apply_changes(publication, changes)
        publication.updated_at = utc_now()
        self._session.flush()
        return publication
