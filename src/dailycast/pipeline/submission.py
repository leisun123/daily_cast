"""Durable TaskRun submission with database-backed idempotency."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from dailycast.core.hashes import sha256_text
from dailycast.core.identifiers import UUIDGenerator
from dailycast.core.time import Clock
from dailycast.db.models import TaskRun, TaskRunStatus
from dailycast.db.repositories import TaskRunRepository
from dailycast.db.transactions import UnitOfWork
from dailycast.pipeline.contracts import TaskCommand, canonical_json
from dailycast.pipeline.state import validate_task_run_transition


class TaskEnqueuer(Protocol):
    """The only queue capability a submission service needs."""

    def enqueue(self, task_run_id: str) -> bool:
        """Offer an already committed TaskRun identifier and report local acceptance."""


class IdempotencyConflictError(ValueError):
    """A client reused one idempotency key with a different task request."""


class TaskSubmissionService:
    """Persist a queued run before making it eligible for asynchronous execution."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        enqueuer: TaskEnqueuer,
        *,
        clock: Clock | None = None,
        uuid_generator: UUIDGenerator | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._enqueuer = enqueuer
        self._clock = clock or Clock()
        self._uuid_generator = uuid_generator or UUIDGenerator()
        self._offered_task_run_ids: set[str] = set()

    def submit(self, command: TaskCommand) -> TaskRun:
        """Create or reuse a TaskRun, committing before enqueueing a new row."""
        normalized = self._normalize(command)
        created = False
        try:
            with UnitOfWork(self._session_factory) as unit:
                assert unit.session is not None
                repository = TaskRunRepository(unit.session)
                task_run = self._find_idempotent_or_active(repository, normalized)
                if task_run is None:
                    task_run = repository.create(**normalized.create_values())
                    created = True
        except IntegrityError:
            task_run = self._resolve_insert_race(normalized)
            created = False

        if created:
            # The UnitOfWork has committed successfully at this point.  SQLite is source of truth.
            self._enqueue_once(task_run.id)
        return task_run

    def enqueue_existing(self, task_run_id: str) -> None:
        """Re-offer a durable queued row discovered by startup recovery."""
        self._enqueue_once(task_run_id)

    def recover_stale(self, task_run_id: str, *, stale_before: datetime) -> TaskRun | None:
        """Atomically interrupt a stale run and create its single queued recovery child."""
        recovery_key = f"recovery:{task_run_id}"
        created = False
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            repository = TaskRunRepository(unit.session)
            parent = repository.get(task_run_id)
            if parent is None or not self._is_stale_running(parent, stale_before):
                return None

            now = self._clock.now()
            if parent.deadline_at is not None and _as_utc(parent.deadline_at) <= _as_utc(now):
                validate_task_run_transition(parent.status, TaskRunStatus.TIMED_OUT)
                repository.update_status(
                    parent,
                    TaskRunStatus.TIMED_OUT,
                    ended_at=now,
                    error_code="TASK_DEADLINE_EXCEEDED",
                    error_summary="task deadline elapsed before stale-worker recovery",
                    retryable=False,
                )
                return None

            existing = repository.get_by_idempotency_key(recovery_key)
            validate_task_run_transition(parent.status, TaskRunStatus.INTERRUPTED)
            repository.update_status(
                parent,
                TaskRunStatus.INTERRUPTED,
                ended_at=now,
                error_code="TASK_INTERRUPTED",
                error_summary="worker heartbeat expired before startup recovery",
            )
            if existing is None:
                child = repository.create(
                    id=str(self._uuid_generator.new()),
                    task_type=parent.task_type,
                    business_key=parent.business_key,
                    idempotency_key=recovery_key,
                    trigger_type="retry",
                    status=TaskRunStatus.QUEUED,
                    episode_id=parent.episode_id,
                    parent_task_run_id=parent.id,
                    pipeline_version=parent.pipeline_version,
                    config_fingerprint=parent.config_fingerprint,
                    config_snapshot_json=parent.config_snapshot_json,
                    request_json=parent.request_json,
                    deadline_at=parent.deadline_at,
                    retryable=parent.retryable,
                    created_at=now,
                    updated_at=now,
                )
                created = True
            else:
                child = existing

        if created:
            self._enqueue_once(child.id)
        return child

    def _enqueue_once(self, task_run_id: str) -> None:
        """Avoid duplicate in-process offers while SQLite remains the durable recovery source."""
        if task_run_id in self._offered_task_run_ids:
            return
        if self._enqueuer.enqueue(task_run_id):
            self._offered_task_run_ids.add(task_run_id)

    def _find_idempotent_or_active(
        self, repository: TaskRunRepository, normalized: _NormalizedTaskCommand
    ) -> TaskRun | None:
        existing = repository.get_by_idempotency_key(normalized.idempotency_key)
        if existing is not None:
            self._assert_same_request(existing, normalized)
            return existing
        return repository.get_active_by_business_key(normalized.business_key)

    def _resolve_insert_race(self, normalized: _NormalizedTaskCommand) -> TaskRun:
        """Return the winner of a SQLite uniqueness race without duplicating queue work."""
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            repository = TaskRunRepository(unit.session)
            existing = repository.get_by_idempotency_key(normalized.idempotency_key)
            if existing is not None:
                self._assert_same_request(existing, normalized)
                return existing
            active = repository.get_active_by_business_key(normalized.business_key)
            if active is not None:
                return active
        msg = "TaskRun insert failed without a reusable idempotency or active-business-key winner"
        raise RuntimeError(msg)

    def _normalize(self, command: TaskCommand) -> _NormalizedTaskCommand:
        """Canonicalize non-secret request/configuration data and derive durable identities."""
        request_json = canonical_json(command.request)
        config_snapshot_json = canonical_json(command.config_snapshot)
        business_key = command.business_key or self._business_key(command, request_json)
        idempotency_key = command.idempotency_key or sha256_text(
            "\n".join(
                (
                    business_key,
                    command.task_type.value,
                    command.trigger_type.value,
                    command.pipeline_version,
                    request_json,
                    config_snapshot_json,
                )
            )
        )
        now = self._clock.now()
        return _NormalizedTaskCommand(
            id=str(self._uuid_generator.new()),
            task_type=command.task_type.value,
            business_key=business_key,
            idempotency_key=idempotency_key,
            trigger_type=command.trigger_type.value,
            episode_id=command.episode_id,
            parent_task_run_id=command.parent_task_run_id,
            pipeline_version=command.pipeline_version,
            config_fingerprint=sha256_text(config_snapshot_json),
            config_snapshot_json=config_snapshot_json,
            request_json=request_json,
            deadline_at=command.deadline_at,
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def _business_key(command: TaskCommand, request_json: str) -> str:
        """Derive the documented daily key or a stable generic task identity."""
        if command.task_type.value == "daily_generate":
            episode_date = command.request.get("episode_date")
            edition = command.request.get("edition", "daily")
            if isinstance(episode_date, str) and isinstance(edition, str):
                return f"daily:{episode_date}:{edition}:{command.pipeline_version}"
            msg = "daily_generate requires string request fields episode_date and optional edition"
            raise ValueError(msg)
        return f"{command.task_type.value}:{sha256_text(request_json)}:{command.pipeline_version}"

    @staticmethod
    def _assert_same_request(existing: TaskRun, normalized: _NormalizedTaskCommand) -> None:
        """Reject an idempotency key that was previously bound to another request."""
        same_request = (
            existing.task_type.value == normalized.task_type
            and existing.business_key == normalized.business_key
            and existing.pipeline_version == normalized.pipeline_version
            and existing.request_json == normalized.request_json
            and existing.config_snapshot_json == normalized.config_snapshot_json
        )
        if not same_request:
            msg = "idempotency_key is already bound to a different task request"
            raise IdempotencyConflictError(msg)

    @staticmethod
    def _is_stale_running(task_run: TaskRun, stale_before: datetime) -> bool:
        """Treat only a running task with an expired (or absent old) heartbeat as stale."""
        if task_run.status != TaskRunStatus.RUNNING:
            return False
        if task_run.heartbeat_at is not None:
            return _as_utc(task_run.heartbeat_at) < _as_utc(stale_before)
        return task_run.started_at is not None and _as_utc(task_run.started_at) < _as_utc(
            stale_before
        )


def _as_utc(value: datetime) -> datetime:
    """Normalize SQLite-returned naive timestamps before comparing them to UTC timestamps."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class _NormalizedTaskCommand:
    """Internal normalized form containing exactly the TaskRun creation values."""

    def __init__(
        self,
        *,
        id: str,
        task_type: str,
        business_key: str,
        idempotency_key: str,
        trigger_type: str,
        episode_id: int | None,
        parent_task_run_id: str | None,
        pipeline_version: str,
        config_fingerprint: str,
        config_snapshot_json: str,
        request_json: str,
        deadline_at: datetime | None,
        created_at: datetime,
        updated_at: datetime,
    ) -> None:
        self.id = id
        self.task_type = task_type
        self.business_key = business_key
        self.idempotency_key = idempotency_key
        self.trigger_type = trigger_type
        self.episode_id = episode_id
        self.parent_task_run_id = parent_task_run_id
        self.pipeline_version = pipeline_version
        self.config_fingerprint = config_fingerprint
        self.config_snapshot_json = config_snapshot_json
        self.request_json = request_json
        self.deadline_at = deadline_at
        self.created_at = created_at
        self.updated_at = updated_at

    def create_values(self) -> dict[str, object]:
        """Return the database values for the first queued attempt of this command."""
        return {
            "id": self.id,
            "task_type": self.task_type,
            "business_key": self.business_key,
            "idempotency_key": self.idempotency_key,
            "trigger_type": self.trigger_type,
            "status": TaskRunStatus.QUEUED,
            "episode_id": self.episode_id,
            "parent_task_run_id": self.parent_task_run_id,
            "pipeline_version": self.pipeline_version,
            "config_fingerprint": self.config_fingerprint,
            "config_snapshot_json": self.config_snapshot_json,
            "request_json": self.request_json,
            "deadline_at": self.deadline_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
