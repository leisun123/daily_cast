"""Sprint 2 task submission, execution, recovery, and state tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dailycast.core.config import load_settings
from dailycast.db.models import TaskRun, TaskRunStatus, TaskType, TriggerType
from dailycast.db.repositories import TaskRunRepository
from dailycast.db.revision import build_alembic_config
from dailycast.db.session import create_session_factory, create_sqlite_engine
from dailycast.db.transactions import UnitOfWork
from dailycast.pipeline.context import PipelineContext
from dailycast.pipeline.contracts import PipelineStep, StepResult, TaskCommand
from dailycast.pipeline.executor import InProcessTaskExecutor
from dailycast.pipeline.orchestrator import PipelineOrchestrator
from dailycast.pipeline.recovery import RecoveryService
from dailycast.pipeline.state import TaskStateTransitionError, validate_task_run_transition
from dailycast.pipeline.submission import IdempotencyConflictError, TaskSubmissionService


class RecordingExecutor:
    """Record persisted task identifiers without executing a pipeline."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self.task_run_ids: list[str] = []

    def enqueue(self, task_run_id: str) -> None:
        """Assert that an enqueued task is already visible in a fresh transaction."""
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            assert TaskRunRepository(unit.session).get(task_run_id) is not None
        self.task_run_ids.append(task_run_id)


@pytest.fixture
def migrated_session_factory(app_config_path: Path) -> sessionmaker[Session]:
    """Build an isolated task database through the real Alembic upgrade path."""
    settings = load_settings(config_path=app_config_path)
    ini_path = Path(__file__).resolve().parents[1] / "alembic.ini"
    command.upgrade(
        build_alembic_config(ini_path=ini_path, database_url=settings.database.url), "head"
    )
    engine = create_sqlite_engine(settings.database)
    factory = create_session_factory(engine)
    try:
        yield factory
    finally:
        engine.dispose()


def task_command(
    *,
    episode_date: str = "2026-07-22",
    idempotency_key: str | None = None,
    trigger_type: TriggerType = TriggerType.MANUAL,
) -> TaskCommand:
    """Create the smallest supported test-only daily task command."""
    return TaskCommand(
        task_type=TaskType.DAILY_GENERATE,
        request={"edition": "daily", "episode_date": episode_date},
        config_snapshot={"pipeline": "test"},
        pipeline_version="test-v1",
        idempotency_key=idempotency_key,
        trigger_type=trigger_type,
    )


def get_task_run(factory: sessionmaker[Session], task_run_id: str) -> Any:
    """Read one detached task run after its short transaction completes."""
    with UnitOfWork(factory) as unit:
        assert unit.session is not None
        task_run = TaskRunRepository(unit.session).get(task_run_id)
        assert task_run is not None
        return task_run


def test_submission_persists_before_enqueue(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """A queued TaskRun commits before the executor receives its identifier."""
    executor = RecordingExecutor(migrated_session_factory)
    service = TaskSubmissionService(migrated_session_factory, executor)

    submitted = service.submit(task_command(idempotency_key="submit-one"))

    assert submitted.status == TaskRunStatus.QUEUED
    assert executor.task_run_ids == [submitted.id]
    assert get_task_run(migrated_session_factory, submitted.id).business_key == (
        "daily:2026-07-22:daily:test-v1"
    )


def test_submission_returns_active_run_for_duplicate_business_key(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """Different submitters competing for the same daily job receive one active run."""
    executor = RecordingExecutor(migrated_session_factory)
    service = TaskSubmissionService(migrated_session_factory, executor)

    first = service.submit(task_command(idempotency_key="daily-first"))
    duplicate = service.submit(task_command(idempotency_key="daily-second"))

    assert duplicate.id == first.id
    assert executor.task_run_ids == [first.id]


def test_submission_rejects_reused_idempotency_key_with_different_request(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """One idempotency key cannot be rebound to a different normalized request."""
    service = TaskSubmissionService(
        migrated_session_factory, RecordingExecutor(migrated_session_factory)
    )
    service.submit(task_command(idempotency_key="same-key"))

    with pytest.raises(IdempotencyConflictError):
        service.submit(task_command(episode_date="2026-07-23", idempotency_key="same-key"))


class DelayStep:
    """Test-only non-business checkpoint used to exercise executor mechanics."""

    def __init__(self, name: str, delay_seconds: float) -> None:
        self.name = name
        self._delay_seconds = delay_seconds

    async def run(self, context: PipelineContext) -> StepResult:
        """Pause without creating business data."""
        del context
        await asyncio.sleep(self._delay_seconds)
        return StepResult()


def executor_test_pipeline(step_delay_seconds: float) -> tuple[PipelineStep, ...]:
    """Create test-local checkpoint instances after production fake steps were removed."""
    return (
        DelayStep("test_collecting", step_delay_seconds),
        DelayStep("test_processing", step_delay_seconds),
        DelayStep("test_finished", step_delay_seconds),
    )


def test_fake_pipeline_executes_and_records_steps(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """The single worker runs each fake checkpoint and completes the TaskRun."""

    async def scenario() -> None:
        orchestrator = PipelineOrchestrator(
            migrated_session_factory, executor_test_pipeline(step_delay_seconds=0.005)
        )
        executor = InProcessTaskExecutor(migrated_session_factory, orchestrator)
        service = TaskSubmissionService(migrated_session_factory, executor)
        await executor.start()
        submitted = service.submit(task_command(idempotency_key="execute-success"))
        try:
            for _ in range(100):
                task_run = get_task_run(migrated_session_factory, submitted.id)
                if task_run.status == TaskRunStatus.SUCCEEDED:
                    break
                await asyncio.sleep(0.01)
            assert (
                get_task_run(migrated_session_factory, submitted.id).status
                == TaskRunStatus.SUCCEEDED
            )
            with UnitOfWork(migrated_session_factory) as unit:
                assert unit.session is not None
                task_run = TaskRunRepository(unit.session).get(submitted.id)
                assert task_run is not None
                assert [step.step_name for step in task_run.steps] == [
                    "test_collecting",
                    "test_processing",
                    "test_finished",
                ]
                assert all(step.status == "succeeded" for step in task_run.steps)
        finally:
            await executor.shutdown(grace_seconds=1)

    asyncio.run(scenario())


def test_running_task_heartbeat_is_updated_independently(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """A running fake task receives periodic committed heartbeat updates."""

    async def scenario() -> None:
        orchestrator = PipelineOrchestrator(
            migrated_session_factory, executor_test_pipeline(step_delay_seconds=0.08)
        )
        executor = InProcessTaskExecutor(
            migrated_session_factory, orchestrator, heartbeat_interval_seconds=0.01
        )
        service = TaskSubmissionService(migrated_session_factory, executor)
        await executor.start()
        submitted = service.submit(task_command(idempotency_key="heartbeat"))
        try:
            for _ in range(50):
                running = get_task_run(migrated_session_factory, submitted.id)
                if running.status == TaskRunStatus.RUNNING:
                    break
                await asyncio.sleep(0.005)
            first_heartbeat = get_task_run(migrated_session_factory, submitted.id).heartbeat_at
            assert first_heartbeat is not None
            await asyncio.sleep(0.03)
            assert (
                get_task_run(migrated_session_factory, submitted.id).heartbeat_at > first_heartbeat
            )
        finally:
            await executor.shutdown(grace_seconds=1)

    asyncio.run(scenario())


def test_shutdown_leaves_not_started_queued_tasks_for_recovery(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """Graceful shutdown interrupts only active work and leaves later queued rows durable."""

    async def scenario() -> None:
        orchestrator = PipelineOrchestrator(
            migrated_session_factory, executor_test_pipeline(step_delay_seconds=0.08)
        )
        executor = InProcessTaskExecutor(migrated_session_factory, orchestrator)
        service = TaskSubmissionService(migrated_session_factory, executor)
        await executor.start()
        first = service.submit(task_command(idempotency_key="shutdown-active"))
        second = service.submit(
            task_command(episode_date="2026-07-23", idempotency_key="shutdown-queued")
        )
        for _ in range(50):
            if get_task_run(migrated_session_factory, first.id).status == TaskRunStatus.RUNNING:
                break
            await asyncio.sleep(0.005)
        await executor.shutdown(grace_seconds=1)
        assert get_task_run(migrated_session_factory, first.id).status == TaskRunStatus.INTERRUPTED
        assert get_task_run(migrated_session_factory, second.id).status == TaskRunStatus.QUEUED

    asyncio.run(scenario())


def test_recovery_enqueues_queued_run_and_creates_one_child_for_stale_run(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """Startup recovery preserves queued work and idempotently resumes stale running work."""
    now = datetime.now(UTC)
    stale_id = str(uuid4())
    queued_id = str(uuid4())
    with UnitOfWork(migrated_session_factory) as unit:
        assert unit.session is not None
        repository = TaskRunRepository(unit.session)
        repository.create(
            id=queued_id,
            task_type=TaskType.DAILY_GENERATE,
            business_key="daily:2026-07-20:daily:test-v1",
            idempotency_key="queued-existing",
            trigger_type=TriggerType.MANUAL,
            status=TaskRunStatus.QUEUED,
            pipeline_version="test-v1",
            config_fingerprint="a" * 64,
            config_snapshot_json='{"pipeline":"test"}',
            request_json='{"edition":"daily","episode_date":"2026-07-20"}',
            created_at=now,
            updated_at=now,
        )
        repository.create(
            id=stale_id,
            task_type=TaskType.DAILY_GENERATE,
            business_key="daily:2026-07-21:daily:test-v1",
            idempotency_key="stale-existing",
            trigger_type=TriggerType.MANUAL,
            status=TaskRunStatus.RUNNING,
            pipeline_version="test-v1",
            config_fingerprint="a" * 64,
            config_snapshot_json='{"pipeline":"test"}',
            request_json='{"edition":"daily","episode_date":"2026-07-21"}',
            heartbeat_at=now - timedelta(seconds=61),
            started_at=now - timedelta(seconds=120),
            created_at=now - timedelta(seconds=120),
            updated_at=now - timedelta(seconds=61),
        )

    executor = RecordingExecutor(migrated_session_factory)
    submission = TaskSubmissionService(migrated_session_factory, executor)
    recovery = RecoveryService(migrated_session_factory, submission, stale_after_seconds=60)

    asyncio.run(recovery.recover())
    asyncio.run(recovery.recover())

    stale = get_task_run(migrated_session_factory, stale_id)
    assert stale.status == TaskRunStatus.INTERRUPTED
    with UnitOfWork(migrated_session_factory) as unit:
        assert unit.session is not None
        recovered = list(
            unit.session.scalars(select(TaskRun).where(TaskRun.parent_task_run_id == stale_id))
        )
    assert len(recovered) == 1
    assert recovered[0].status == TaskRunStatus.QUEUED
    assert recovered[0].parent_task_run_id == stale_id
    assert queued_id in executor.task_run_ids
    assert executor.task_run_ids.count(recovered[0].id) == 1


def test_task_run_state_validation_rejects_invalid_transition() -> None:
    """Only the Sprint 2 TaskRun transitions are accepted by the state validator."""
    validate_task_run_transition(TaskRunStatus.QUEUED, TaskRunStatus.RUNNING)
    validate_task_run_transition(TaskRunStatus.INTERRUPTED, TaskRunStatus.QUEUED)

    with pytest.raises(TaskStateTransitionError):
        validate_task_run_transition(TaskRunStatus.QUEUED, TaskRunStatus.FAILED)
