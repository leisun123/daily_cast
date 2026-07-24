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
from dailycast.core.errors import AIBudgetExceededError, LLMProviderTimeoutError
from dailycast.db.models import (
    AudioSegment,
    Episode,
    TaskRun,
    TaskRunStatus,
    TaskStepStatus,
    TaskType,
    TriggerType,
)
from dailycast.db.repositories import TaskRunRepository, TaskStepRepository
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

    def enqueue(self, task_run_id: str) -> bool:
        """Assert that an enqueued task is already visible in a fresh transaction."""
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            assert TaskRunRepository(unit.session).get(task_run_id) is not None
        self.task_run_ids.append(task_run_id)
        return True


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


class CheckingReviseStep:
    """Record the review outcome that prevents Episode creation."""

    name = "checking"

    async def run(self, context: PipelineContext) -> StepResult:
        """Keep the review artifact outcome while allowing its gate to run next."""
        del context
        return StepResult(
            input_count=1,
            output_count=0,
            warning_count=1,
            details={"review_verdict": "revise"},
        )


class ReviewGatedCreateEpisodeStep:
    """Model the real create_episode review gate without manufacturing business data."""

    name = "create_episode"

    async def run(self, context: PipelineContext) -> StepResult:
        """Stop normally and leave the editorial artifacts available for human revision."""
        del context
        return StepResult(
            input_count=1,
            output_count=0,
            warning_count=1,
            stop_pipeline=True,
            terminal_status=TaskRunStatus.WAITING_ACTION,
            completion_code="EDITORIAL_REVIEW_NOT_PASS",
            completion_summary="editorial review verdict requires human revision",
        )


class UnexpectedAudioStep:
    """Fail loudly if a waiting-action run ever reaches audio generation."""

    name = "generate_audio"

    def __init__(self) -> None:
        self.called = False

    async def run(self, context: PipelineContext) -> StepResult:
        """Expose any regression in terminal pipeline control flow."""
        del context
        self.called = True
        raise AssertionError("generate_audio must not run after editorial review requests revision")


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


def test_revise_review_waits_for_human_action_without_episode_or_audio(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """A revise verdict ends normally before Episode and audio-dependent checkpoints."""

    async def scenario() -> None:
        audio_step = UnexpectedAudioStep()
        orchestrator = PipelineOrchestrator(
            migrated_session_factory,
            (CheckingReviseStep(), ReviewGatedCreateEpisodeStep(), audio_step),
        )
        executor = RecordingExecutor(migrated_session_factory)
        submitted = TaskSubmissionService(migrated_session_factory, executor).submit(
            task_command(idempotency_key="review-revise-waits")
        )

        completed = await orchestrator.execute(submitted.id)

        assert completed is not None
        assert completed.status is TaskRunStatus.WAITING_ACTION
        assert completed.error_code == "EDITORIAL_REVIEW_NOT_PASS"
        assert audio_step.called is False
        with UnitOfWork(migrated_session_factory) as unit:
            assert unit.session is not None
            task_run = TaskRunRepository(unit.session).get(submitted.id)
            assert task_run is not None
            assert [step.step_name for step in task_run.steps] == ["checking", "create_episode"]
            assert unit.session.scalars(select(Episode)).all() == []
            assert unit.session.scalars(select(AudioSegment)).all() == []

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


def test_stale_running_task_past_deadline_is_timed_out_without_recovery_child(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """Crash recovery must not resurrect work whose durable deadline has already elapsed."""
    now = datetime.now(UTC)
    task_run_id = str(uuid4())
    with UnitOfWork(migrated_session_factory) as unit:
        assert unit.session is not None
        TaskRunRepository(unit.session).create(
            id=task_run_id,
            task_type=TaskType.DAILY_GENERATE,
            business_key="daily:2026-07-18:daily:test-v1",
            idempotency_key="expired-stale-existing",
            trigger_type=TriggerType.MANUAL,
            status=TaskRunStatus.RUNNING,
            pipeline_version="test-v1",
            config_fingerprint="a" * 64,
            config_snapshot_json='{"pipeline":"test"}',
            request_json='{"edition":"daily","episode_date":"2026-07-18"}',
            deadline_at=now - timedelta(seconds=1),
            heartbeat_at=now - timedelta(seconds=61),
            started_at=now - timedelta(seconds=120),
            created_at=now - timedelta(seconds=120),
            updated_at=now - timedelta(seconds=61),
        )

    submission = TaskSubmissionService(
        migrated_session_factory, RecordingExecutor(migrated_session_factory)
    )
    recovered = submission.recover_stale(task_run_id, stale_before=now - timedelta(seconds=60))

    assert recovered is None
    task_run = get_task_run(migrated_session_factory, task_run_id)
    assert task_run.status is TaskRunStatus.TIMED_OUT
    assert task_run.error_code == "TASK_DEADLINE_EXCEEDED"
    with UnitOfWork(migrated_session_factory) as unit:
        assert unit.session is not None
        children = list(
            unit.session.scalars(select(TaskRun).where(TaskRun.parent_task_run_id == task_run_id))
        )
    assert children == []


def test_task_run_state_validation_rejects_invalid_transition() -> None:
    """Only the Sprint 2 TaskRun transitions are accepted by the state validator."""
    validate_task_run_transition(TaskRunStatus.QUEUED, TaskRunStatus.RUNNING)
    validate_task_run_transition(TaskRunStatus.INTERRUPTED, TaskRunStatus.QUEUED)
    validate_task_run_transition(TaskRunStatus.RUNNING, TaskRunStatus.WAITING_ACTION)

    with pytest.raises(TaskStateTransitionError):
        validate_task_run_transition(TaskRunStatus.QUEUED, TaskRunStatus.FAILED)


class _CrashOnceOrchestrator:
    """Test double that exposes executor-level failures outside PipelineOrchestrator."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(
        self, task_run_id: str, shutdown_requested: asyncio.Event | None = None
    ) -> None:
        """Crash the first dispatched item and accept the next one."""
        del shutdown_requested
        self.calls.append(task_run_id)
        if len(self.calls) == 1:
            raise RuntimeError("unexpected worker boundary failure")


def test_worker_isolates_one_unexpected_task_exception_and_stays_ready(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """One broken task must not kill the sole worker or make readiness lie."""

    async def scenario() -> None:
        crashing = _CrashOnceOrchestrator()
        executor = InProcessTaskExecutor(migrated_session_factory, crashing)  # type: ignore[arg-type]
        await executor.start()
        try:
            assert executor.enqueue("first") is True
            assert executor.enqueue("second") is True
            for _ in range(100):
                if crashing.calls == ["first", "second"]:
                    break
                await asyncio.sleep(0.01)
            assert crashing.calls == ["first", "second"]
            assert executor.is_healthy is True
        finally:
            await executor.shutdown(grace_seconds=1)

    asyncio.run(scenario())


def test_supervisor_restarts_a_worker_loop_that_crashes(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """A worker implementation crash must be restarted before readiness returns healthy."""

    class CrashOnceExecutor(InProcessTaskExecutor):
        def __init__(self) -> None:
            super().__init__(migrated_session_factory, _CrashOnceOrchestrator())  # type: ignore[arg-type]
            self.worker_start_count = 0

        async def _worker_loop(self) -> None:
            self.worker_start_count += 1
            if self.worker_start_count == 1:
                raise RuntimeError("worker-loop-crash")
            await self._shutdown_requested.wait()

    async def scenario() -> None:
        executor = CrashOnceExecutor()
        await executor.start()
        try:
            for _ in range(100):
                if executor.worker_start_count >= 2 and executor.is_healthy:
                    break
                await asyncio.sleep(0.01)
            assert executor.worker_start_count == 2
            assert executor.is_healthy is True
            assert executor.readiness_detail == "single worker supervisor is running"
        finally:
            await executor.shutdown(grace_seconds=1)

    asyncio.run(scenario())


def test_queue_full_task_is_redelivered_from_sqlite_after_capacity_frees(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """A committed TaskRun rejected by the bounded queue is not stranded until restart."""

    async def scenario() -> None:
        orchestrator = PipelineOrchestrator(
            migrated_session_factory, executor_test_pipeline(step_delay_seconds=0.01)
        )
        executor = InProcessTaskExecutor(
            migrated_session_factory,
            orchestrator,
            queue_maxsize=1,
            redelivery_interval_seconds=0.01,
        )
        service = TaskSubmissionService(migrated_session_factory, executor)
        first = service.submit(task_command(idempotency_key="queue-full-first"))
        second = service.submit(
            task_command(episode_date="2026-07-23", idempotency_key="queue-full-second")
        )
        assert get_task_run(migrated_session_factory, second.id).status is TaskRunStatus.QUEUED
        await executor.start()
        try:
            for _ in range(250):
                if (
                    get_task_run(migrated_session_factory, first.id).status
                    is TaskRunStatus.SUCCEEDED
                    and get_task_run(migrated_session_factory, second.id).status
                    is TaskRunStatus.SUCCEEDED
                ):
                    break
                await asyncio.sleep(0.01)
            assert (
                get_task_run(migrated_session_factory, first.id).status is TaskRunStatus.SUCCEEDED
            )
            assert (
                get_task_run(migrated_session_factory, second.id).status is TaskRunStatus.SUCCEEDED
            )
        finally:
            await executor.shutdown(grace_seconds=1)

    asyncio.run(scenario())


def test_expired_task_is_marked_timed_out_without_running_a_step(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """A deadline is a durable execution boundary rather than a best-effort advisory."""

    async def scenario() -> None:
        called = False

        class MustNotRun:
            name = "must_not_run"

            async def run(self, context: PipelineContext) -> StepResult:
                nonlocal called
                del context
                called = True
                return StepResult()

        submitted = TaskSubmissionService(
            migrated_session_factory, RecordingExecutor(migrated_session_factory)
        ).submit(
            TaskCommand(
                task_type=TaskType.DAILY_GENERATE,
                request={"edition": "daily", "episode_date": "2026-07-24"},
                config_snapshot={"pipeline": "test"},
                pipeline_version="test-v1",
                idempotency_key="expired-task",
                deadline_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        await PipelineOrchestrator(migrated_session_factory, (MustNotRun(),)).execute(submitted.id)
        task_run = get_task_run(migrated_session_factory, submitted.id)
        assert called is False
        assert task_run.status is TaskRunStatus.TIMED_OUT
        assert task_run.error_code == "TASK_DEADLINE_EXCEEDED"
        assert task_run.retryable is False

    asyncio.run(scenario())


def test_recovery_child_restores_checkpoint_and_skips_completed_step(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """A stale-run child starts after durable checkpoints instead of repeating prior work."""

    class CompletedStep:
        name = "completed"

        async def run(self, context: PipelineContext) -> StepResult:
            raise AssertionError("durable completed checkpoint must not run again")

        def restore_checkpoint(
            self, context: PipelineContext, checkpoint: dict[str, object]
        ) -> None:
            context.values["checkpoint_value"] = checkpoint["checkpoint_value"]

    class RemainingStep:
        name = "remaining"

        async def run(self, context: PipelineContext) -> StepResult:
            assert context.values["checkpoint_value"] == "durable"
            return StepResult(checkpoint_json='{"done":true}')

    now = datetime.now(UTC)
    parent_id = str(uuid4())
    with UnitOfWork(migrated_session_factory) as unit:
        assert unit.session is not None
        run = TaskRunRepository(unit.session).create(
            id=parent_id,
            task_type=TaskType.DAILY_GENERATE,
            business_key="daily:2026-07-19:daily:test-v1",
            idempotency_key="checkpoint-parent",
            trigger_type=TriggerType.MANUAL,
            status=TaskRunStatus.RUNNING,
            pipeline_version="test-v1",
            config_fingerprint="a" * 64,
            config_snapshot_json="{}",
            request_json='{"edition":"daily","episode_date":"2026-07-19"}',
            started_at=now - timedelta(minutes=3),
            heartbeat_at=now - timedelta(minutes=2),
            created_at=now - timedelta(minutes=3),
            updated_at=now - timedelta(minutes=2),
        )
        TaskStepRepository(unit.session).create(
            task_run_id=run.id,
            step_name="completed",
            step_order=1,
            attempt=1,
            status=TaskStepStatus.SUCCEEDED,
            checkpoint_json='{"checkpoint_value":"durable"}',
            details_json="{}",
        )

    enqueuer = RecordingExecutor(migrated_session_factory)
    submission = TaskSubmissionService(migrated_session_factory, enqueuer)
    child = submission.recover_stale(parent_id, stale_before=now - timedelta(seconds=60))
    assert child is not None
    completed = asyncio.run(
        PipelineOrchestrator(migrated_session_factory, (CompletedStep(), RemainingStep())).execute(
            child.id
        )
    )
    assert completed is not None
    assert completed.status is TaskRunStatus.SUCCEEDED
    with UnitOfWork(migrated_session_factory) as unit:
        assert unit.session is not None
        child_steps = TaskRunRepository(unit.session).get(child.id).steps  # type: ignore[union-attr]
        assert [step.step_name for step in child_steps] == ["remaining"]


def test_checkpoint_recovery_prefers_the_newest_step_in_parent_child_lineage(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """A child checkpoint must override the same named root checkpoint regardless of UUID sort."""

    class CompletedStep:
        name = "completed"

        async def run(self, context: PipelineContext) -> StepResult:
            raise AssertionError("the newest durable checkpoint must be restored, not rerun")

        def restore_checkpoint(
            self, context: PipelineContext, checkpoint: dict[str, object]
        ) -> None:
            context.values["checkpoint_value"] = checkpoint["checkpoint_value"]

    class RemainingStep:
        name = "remaining"

        async def run(self, context: PipelineContext) -> StepResult:
            assert context.values["checkpoint_value"] == "child"
            return StepResult()

    now = datetime.now(UTC)
    root_id = "z" * 36
    child_id = "a" * 36
    with UnitOfWork(migrated_session_factory) as unit:
        assert unit.session is not None
        runs = TaskRunRepository(unit.session)
        root = runs.create(
            id=root_id,
            task_type=TaskType.DAILY_GENERATE,
            business_key="daily:2026-07-17:daily:test-v1",
            idempotency_key="checkpoint-root",
            trigger_type=TriggerType.MANUAL,
            status=TaskRunStatus.INTERRUPTED,
            pipeline_version="test-v1",
            config_fingerprint="a" * 64,
            config_snapshot_json="{}",
            request_json='{"edition":"daily","episode_date":"2026-07-17"}',
            created_at=now - timedelta(minutes=2),
            updated_at=now - timedelta(minutes=1),
        )
        runs.create(
            id=child_id,
            task_type=TaskType.DAILY_GENERATE,
            business_key="daily:2026-07-17:daily:test-v1",
            idempotency_key="checkpoint-child",
            trigger_type=TriggerType.RETRY,
            status=TaskRunStatus.QUEUED,
            parent_task_run_id=root.id,
            pipeline_version="test-v1",
            config_fingerprint="a" * 64,
            config_snapshot_json="{}",
            request_json='{"edition":"daily","episode_date":"2026-07-17"}',
            created_at=now,
            updated_at=now,
        )
        steps = TaskStepRepository(unit.session)
        steps.create(
            task_run_id=root.id,
            step_name="completed",
            step_order=1,
            attempt=1,
            status=TaskStepStatus.SUCCEEDED,
            checkpoint_json='{"checkpoint_value":"root"}',
            details_json="{}",
        )
        steps.create(
            task_run_id=child_id,
            step_name="completed",
            step_order=1,
            attempt=1,
            status=TaskStepStatus.SUCCEEDED,
            checkpoint_json='{"checkpoint_value":"child"}',
            details_json="{}",
        )

    completed = asyncio.run(
        PipelineOrchestrator(migrated_session_factory, (CompletedStep(), RemainingStep())).execute(
            child_id
        )
    )
    assert completed is not None
    assert completed.status is TaskRunStatus.SUCCEEDED


def test_checkpoint_recovery_reruns_a_step_failed_by_the_latest_child_attempt(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """An inherited success cannot hide a newer failed child attempt of the same step."""

    class ReplayedStep:
        name = "replayed"

        def __init__(self) -> None:
            self.calls = 0

        async def run(self, context: PipelineContext) -> StepResult:
            self.calls += 1
            context.values["replayed"] = True
            return StepResult()

    class RemainingStep:
        name = "remaining"

        async def run(self, context: PipelineContext) -> StepResult:
            assert context.values["replayed"] is True
            return StepResult()

    now = datetime.now(UTC)
    root_id = "z" * 35 + "1"
    child_id = "a" * 35 + "1"
    with UnitOfWork(migrated_session_factory) as unit:
        assert unit.session is not None
        runs = TaskRunRepository(unit.session)
        root = runs.create(
            id=root_id,
            task_type=TaskType.DAILY_GENERATE,
            business_key="daily:2026-07-16:daily:test-v1",
            idempotency_key="failed-checkpoint-root",
            trigger_type=TriggerType.MANUAL,
            status=TaskRunStatus.INTERRUPTED,
            pipeline_version="test-v1",
            config_fingerprint="a" * 64,
            config_snapshot_json="{}",
            request_json='{"edition":"daily","episode_date":"2026-07-16"}',
            created_at=now - timedelta(minutes=2),
            updated_at=now - timedelta(minutes=1),
        )
        runs.create(
            id=child_id,
            task_type=TaskType.DAILY_GENERATE,
            business_key="daily:2026-07-16:daily:test-v1",
            idempotency_key="failed-checkpoint-child",
            trigger_type=TriggerType.RETRY,
            status=TaskRunStatus.QUEUED,
            parent_task_run_id=root.id,
            pipeline_version="test-v1",
            config_fingerprint="a" * 64,
            config_snapshot_json="{}",
            request_json='{"edition":"daily","episode_date":"2026-07-16"}',
            created_at=now,
            updated_at=now,
        )
        steps = TaskStepRepository(unit.session)
        steps.create(
            task_run_id=root.id,
            step_name="replayed",
            step_order=1,
            attempt=1,
            status=TaskStepStatus.SUCCEEDED,
            checkpoint_json="{}",
            details_json="{}",
        )
        steps.create(
            task_run_id=child_id,
            step_name="replayed",
            step_order=1,
            attempt=1,
            status=TaskStepStatus.FAILED,
            checkpoint_json="{}",
            details_json="{}",
        )

    replayed = ReplayedStep()
    completed = asyncio.run(
        PipelineOrchestrator(migrated_session_factory, (replayed, RemainingStep())).execute(
            child_id
        )
    )
    assert completed is not None
    assert completed.status is TaskRunStatus.SUCCEEDED
    assert replayed.calls == 1


def test_checkpoint_recovery_reruns_from_a_malformed_generic_checkpoint(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """A missing durable ID must not let a generic completed step skip its real work."""

    class CollectingStep:
        name = "collecting"

        def __init__(self) -> None:
            self.calls = 0
            self.artifact_run_id: str | None = None

        async def run(self, context: PipelineContext) -> StepResult:
            self.calls += 1
            self.artifact_run_id = context.artifact_run_id
            context.values["collected_article_ids"] = (1,)
            return StepResult(checkpoint_json='{"article_ids":[1]}')

    class RemainingStep:
        name = "remaining"

        async def run(self, context: PipelineContext) -> StepResult:
            assert context.values["collected_article_ids"] == (1,)
            return StepResult()

    now = datetime.now(UTC)
    root_id = "z" * 35 + "2"
    child_id = "a" * 35 + "2"
    with UnitOfWork(migrated_session_factory) as unit:
        assert unit.session is not None
        runs = TaskRunRepository(unit.session)
        root = runs.create(
            id=root_id,
            task_type=TaskType.DAILY_GENERATE,
            business_key="daily:2026-07-15:daily:test-v1",
            idempotency_key="malformed-checkpoint-root",
            trigger_type=TriggerType.MANUAL,
            status=TaskRunStatus.INTERRUPTED,
            pipeline_version="test-v1",
            config_fingerprint="a" * 64,
            config_snapshot_json="{}",
            request_json='{"edition":"daily","episode_date":"2026-07-15"}',
            created_at=now - timedelta(minutes=2),
            updated_at=now - timedelta(minutes=1),
        )
        runs.create(
            id=child_id,
            task_type=TaskType.DAILY_GENERATE,
            business_key="daily:2026-07-15:daily:test-v1",
            idempotency_key="malformed-checkpoint-child",
            trigger_type=TriggerType.RETRY,
            status=TaskRunStatus.QUEUED,
            parent_task_run_id=root.id,
            pipeline_version="test-v1",
            config_fingerprint="a" * 64,
            config_snapshot_json="{}",
            request_json='{"edition":"daily","episode_date":"2026-07-15"}',
            created_at=now,
            updated_at=now,
        )
        TaskStepRepository(unit.session).create(
            task_run_id=root.id,
            step_name="collecting",
            step_order=1,
            attempt=1,
            status=TaskStepStatus.SUCCEEDED,
            checkpoint_json="{}",
            details_json="{}",
        )

    collecting = CollectingStep()
    completed = asyncio.run(
        PipelineOrchestrator(migrated_session_factory, (collecting, RemainingStep())).execute(
            child_id
        )
    )
    assert completed is not None
    assert completed.status is TaskRunStatus.SUCCEEDED
    assert collecting.calls == 1
    assert collecting.artifact_run_id == child_id


def test_checkpoint_recovery_replays_artifact_producing_steps_in_the_child_run(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """Recovery must not skip an outline whose private artifact belongs to an ancestor run."""

    class OutliningStep:
        name = "outlining"

        def __init__(self) -> None:
            self.calls = 0
            self.artifact_run_id: str | None = None

        async def run(self, context: PipelineContext) -> StepResult:
            self.calls += 1
            self.artifact_run_id = context.artifact_run_id
            context.values["outlined_news_event_ids"] = (1,)
            return StepResult(checkpoint_json='{"event_ids":[1]}')

    class FollowingStep:
        name = "following"

        async def run(self, context: PipelineContext) -> StepResult:
            assert context.values["outlined_news_event_ids"] == (1,)
            return StepResult()

    now = datetime.now(UTC)
    root_id = "c" * 35 + "3"
    child_id = "d" * 35 + "3"
    with UnitOfWork(migrated_session_factory) as unit:
        assert unit.session is not None
        runs = TaskRunRepository(unit.session)
        root = runs.create(
            id=root_id,
            task_type=TaskType.DAILY_GENERATE,
            business_key="daily:2026-07-14:daily:test-v1",
            idempotency_key="artifact-checkpoint-root",
            trigger_type=TriggerType.MANUAL,
            status=TaskRunStatus.INTERRUPTED,
            pipeline_version="test-v1",
            config_fingerprint="a" * 64,
            config_snapshot_json="{}",
            request_json='{"edition":"daily","episode_date":"2026-07-14"}',
            created_at=now - timedelta(minutes=2),
            updated_at=now - timedelta(minutes=1),
        )
        runs.create(
            id=child_id,
            task_type=TaskType.DAILY_GENERATE,
            business_key="daily:2026-07-14:daily:test-v1",
            idempotency_key="artifact-checkpoint-child",
            trigger_type=TriggerType.RETRY,
            status=TaskRunStatus.QUEUED,
            parent_task_run_id=root.id,
            pipeline_version="test-v1",
            config_fingerprint="a" * 64,
            config_snapshot_json="{}",
            request_json='{"edition":"daily","episode_date":"2026-07-14"}',
            created_at=now,
            updated_at=now,
        )
        TaskStepRepository(unit.session).create(
            task_run_id=root.id,
            step_name="outlining",
            step_order=7,
            attempt=1,
            status=TaskStepStatus.SUCCEEDED,
            checkpoint_json='{"event_ids":[1]}',
            details_json="{}",
        )

    outlining = OutliningStep()
    completed = asyncio.run(
        PipelineOrchestrator(migrated_session_factory, (outlining, FollowingStep())).execute(
            child_id
        )
    )

    assert completed is not None
    assert completed.status is TaskRunStatus.SUCCEEDED
    assert outlining.calls == 1
    assert outlining.artifact_run_id == child_id


def test_checkpoint_recovery_reruns_when_a_recorded_artifact_is_missing(
    migrated_session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """A successful historical row cannot skip work after its declared output disappears."""

    class RestorableStep:
        name = "restorable"

        def __init__(self) -> None:
            self.calls = 0

        async def run(self, context: PipelineContext) -> StepResult:
            self.calls += 1
            context.values["restorable_output"] = "rerun"
            return StepResult(checkpoint_json='{"result":"rerun"}')

        def restore_checkpoint(
            self, context: PipelineContext, checkpoint: dict[str, object]
        ) -> bool:
            context.values["restorable_output"] = checkpoint["result"]
            return True

    class DownstreamStep:
        name = "downstream"

        async def run(self, context: PipelineContext) -> StepResult:
            assert context.values["restorable_output"] == "rerun"
            return StepResult(checkpoint_json="{}")

    now = datetime.now(UTC)
    parent_id = str(uuid4())
    child_id = str(uuid4())
    with UnitOfWork(migrated_session_factory) as unit:
        assert unit.session is not None
        runs = TaskRunRepository(unit.session)
        parent = runs.create(
            id=parent_id,
            task_type=TaskType.DAILY_GENERATE,
            business_key="daily:2026-07-13:daily:test-v1",
            idempotency_key="missing-artifact-parent",
            trigger_type=TriggerType.MANUAL,
            status=TaskRunStatus.INTERRUPTED,
            pipeline_version="test-v1",
            config_fingerprint="a" * 64,
            config_snapshot_json="{}",
            request_json='{"edition":"daily","episode_date":"2026-07-13"}',
            created_at=now - timedelta(minutes=2),
            updated_at=now - timedelta(minutes=1),
        )
        runs.create(
            id=child_id,
            task_type=TaskType.DAILY_GENERATE,
            business_key=parent.business_key,
            idempotency_key="missing-artifact-child",
            trigger_type=TriggerType.RETRY,
            status=TaskRunStatus.QUEUED,
            parent_task_run_id=parent.id,
            pipeline_version="test-v1",
            config_fingerprint="a" * 64,
            config_snapshot_json="{}",
            request_json=parent.request_json,
            created_at=now,
            updated_at=now,
        )
        TaskStepRepository(unit.session).create(
            task_run_id=parent.id,
            step_name="restorable",
            step_order=1,
            attempt=1,
            status=TaskStepStatus.SUCCEEDED,
            checkpoint_json='{"result":"restored"}',
            details_json="{}",
            artifact_path="work/missing.json",
        )

    restorable = RestorableStep()
    completed = asyncio.run(
        PipelineOrchestrator(
            migrated_session_factory,
            (restorable, DownstreamStep()),
            artifact_roots=(tmp_path,),
        ).execute(child_id)
    )

    assert completed is not None
    assert completed.status is TaskRunStatus.SUCCEEDED
    assert restorable.calls == 1


def test_checkpoint_recovery_reruns_when_output_fingerprint_no_longer_matches(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """A restorer must rerun its checkpoint when current output identity differs from history."""

    class FingerprintedStep:
        name = "fingerprinted"

        def __init__(self) -> None:
            self.calls = 0

        async def run(self, context: PipelineContext) -> StepResult:
            self.calls += 1
            context.values["fingerprinted_output"] = "current"
            return StepResult(checkpoint_json='{"result":"current"}')

        def restore_checkpoint(
            self, context: PipelineContext, checkpoint: dict[str, object]
        ) -> bool:
            context.values["fingerprinted_output"] = checkpoint["result"]
            return True

        def expected_output_fingerprint(
            self, context: PipelineContext, checkpoint: dict[str, object]
        ) -> str:
            del context, checkpoint
            return "current-fingerprint"

    now = datetime.now(UTC)
    parent_id = str(uuid4())
    child_id = str(uuid4())
    with UnitOfWork(migrated_session_factory) as unit:
        assert unit.session is not None
        runs = TaskRunRepository(unit.session)
        parent = runs.create(
            id=parent_id,
            task_type=TaskType.DAILY_GENERATE,
            business_key="daily:2026-07-12:daily:test-v1",
            idempotency_key="fingerprint-parent",
            trigger_type=TriggerType.MANUAL,
            status=TaskRunStatus.INTERRUPTED,
            pipeline_version="test-v1",
            config_fingerprint="a" * 64,
            config_snapshot_json="{}",
            request_json='{"edition":"daily","episode_date":"2026-07-12"}',
            created_at=now - timedelta(minutes=2),
            updated_at=now - timedelta(minutes=1),
        )
        runs.create(
            id=child_id,
            task_type=TaskType.DAILY_GENERATE,
            business_key=parent.business_key,
            idempotency_key="fingerprint-child",
            trigger_type=TriggerType.RETRY,
            status=TaskRunStatus.QUEUED,
            parent_task_run_id=parent.id,
            pipeline_version="test-v1",
            config_fingerprint="a" * 64,
            config_snapshot_json="{}",
            request_json=parent.request_json,
            created_at=now,
            updated_at=now,
        )
        TaskStepRepository(unit.session).create(
            task_run_id=parent.id,
            step_name="fingerprinted",
            step_order=1,
            attempt=1,
            status=TaskStepStatus.SUCCEEDED,
            checkpoint_json='{"result":"historical"}',
            details_json="{}",
            output_fingerprint="historical-fingerprint",
        )

    fingerprinted = FingerprintedStep()
    completed = asyncio.run(
        PipelineOrchestrator(migrated_session_factory, (fingerprinted,)).execute(child_id)
    )

    assert completed is not None
    assert completed.status is TaskRunStatus.SUCCEEDED
    assert fingerprinted.calls == 1


def test_orchestrator_persists_step_and_task_provider_usage(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """Committed step metrics roll up once into the TaskRun audit totals."""

    class UsageStep:
        name = "usage"

        async def run(self, context: PipelineContext) -> StepResult:
            del context
            return StepResult(
                llm_call_count=2,
                llm_input_tokens=31,
                llm_output_tokens=11,
                tts_character_count=17,
            )

    submitted = TaskSubmissionService(
        migrated_session_factory, RecordingExecutor(migrated_session_factory)
    ).submit(task_command(idempotency_key="usage-persistence"))
    completed = asyncio.run(
        PipelineOrchestrator(migrated_session_factory, (UsageStep(),)).execute(submitted.id)
    )
    assert completed is not None
    with UnitOfWork(migrated_session_factory) as unit:
        assert unit.session is not None
        task_run = TaskRunRepository(unit.session).get(submitted.id)
        assert task_run is not None
        step = task_run.steps[0]
        assert (step.llm_call_count, step.llm_input_tokens, step.llm_output_tokens) == (2, 31, 11)
        assert step.tts_character_count == 17
        assert (task_run.llm_call_count, task_run.llm_input_tokens, task_run.llm_output_tokens) == (
            2,
            31,
            11,
        )
        assert task_run.tts_character_count == 17


@pytest.mark.parametrize(
    ("error", "code", "retryable"),
    [
        (AIBudgetExceededError(), "AI_BUDGET_EXCEEDED", False),
        (LLMProviderTimeoutError(), "AI_PROVIDER_TIMEOUT", True),
        (ValueError("bad task input"), "PIPELINE_INPUT_INVALID", False),
    ],
)
def test_pipeline_failure_keeps_error_code_and_retryability_precise(
    migrated_session_factory: sessionmaker[Session],
    error: Exception,
    code: str,
    retryable: bool,
) -> None:
    """Task/step error records distinguish retryable infrastructure from invalid input."""

    class FailingStep:
        name = "failing"

        async def run(self, context: PipelineContext) -> StepResult:
            del context
            raise error

    submitted = TaskSubmissionService(
        migrated_session_factory, RecordingExecutor(migrated_session_factory)
    ).submit(task_command(idempotency_key=f"error-classification-{code}"))
    completed = asyncio.run(
        PipelineOrchestrator(migrated_session_factory, (FailingStep(),)).execute(submitted.id)
    )
    assert completed is not None
    assert completed.status is TaskRunStatus.FAILED
    assert completed.error_code == code
    assert completed.retryable is retryable
    with UnitOfWork(migrated_session_factory) as unit:
        assert unit.session is not None
        task_run = TaskRunRepository(unit.session).get(submitted.id)
        assert task_run is not None
        assert task_run.steps[0].error_code == code
        assert task_run.steps[0].retryable is retryable
