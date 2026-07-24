"""Sequential TaskRun orchestration with durable TaskStep checkpoints."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from sqlalchemy.orm import Session, sessionmaker

from dailycast.core.errors import DailyCastError
from dailycast.core.time import Clock
from dailycast.db.models import TaskRun, TaskRunStatus, TaskStep, TaskStepStatus, TaskType
from dailycast.db.repositories import EpisodeRepository, TaskRunRepository, TaskStepRepository
from dailycast.db.transactions import UnitOfWork
from dailycast.episodes.service import EpisodeService
from dailycast.llm.budget import BudgetController
from dailycast.llm.editorial_service import AIEditorialService
from dailycast.news.service import NewsProcessor
from dailycast.pipeline.context import PipelineContext
from dailycast.pipeline.contracts import PipelineStep, StepResult
from dailycast.pipeline.state import validate_task_run_transition
from dailycast.pipeline.steps.checking import CheckingStep
from dailycast.pipeline.steps.clustering import ClusteringStep
from dailycast.pipeline.steps.collecting import CollectingStep
from dailycast.pipeline.steps.create_episode import CreateEpisodeStep
from dailycast.pipeline.steps.deduplicating import DeduplicatingStep
from dailycast.pipeline.steps.extracting import ExtractingStep
from dailycast.pipeline.steps.filtering import FilteringStep
from dailycast.pipeline.steps.generate_audio import GenerateAudioStep
from dailycast.pipeline.steps.outlining import OutliningStep
from dailycast.pipeline.steps.publish import PublishStep
from dailycast.pipeline.steps.ranking import RankingStep
from dailycast.pipeline.steps.scripting import ScriptingStep
from dailycast.publishing.service import PublicationService
from dailycast.sources.extraction import ContentExtractor
from dailycast.sources.service import ArticleService, SourceCollectionService
from dailycast.tts.service import AudioGenerationService


class PipelineOrchestrator:
    """Run each configured checkpoint sequentially without long-lived SQL transactions."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        steps: Sequence[PipelineStep],
        *,
        clock: Clock | None = None,
        artifact_roots: Sequence[Path] = (),
    ) -> None:
        self._session_factory = session_factory
        self._steps = tuple(steps)
        self._clock = clock or Clock()
        self._artifact_roots = tuple(root.resolve() for root in artifact_roots)

    async def execute(
        self, task_run_id: str, shutdown_requested: asyncio.Event | None = None
    ) -> TaskRun | None:
        """Claim a queued run, persist each step, and return its final durable state."""
        stop_event = shutdown_requested or asyncio.Event()
        if not self._claim(task_run_id):
            return None

        context = PipelineContext(
            task_run_id=task_run_id,
            session_factory=self._session_factory,
            shutdown_requested=stop_event,
            clock=self._clock,
        )
        try:
            warning_count, completed_checkpoints = self._restore_completed_checkpoints(context)
        except Exception as error:
            return self._fail_run_without_step(task_run_id, error)
        for index, step in enumerate(self._steps, start=1):
            if stop_event.is_set():
                return self._interrupt(task_run_id)
            if self._deadline_expired(task_run_id):
                return self._time_out(task_run_id)
            if step.name in completed_checkpoints:
                continue
            task_step_id = self._start_step(task_run_id, step.name, index)
            context.values["active_task_step_id"] = task_step_id
            try:
                result = await step.run(context)
            except Exception as error:
                return self._fail_step_and_run(task_run_id, task_step_id, error)
            self._finish_step(task_step_id, result)
            warning_count += result.warning_count
            if self._deadline_expired(task_run_id):
                return self._time_out(task_run_id)
            if result.terminal_status is not None:
                return self._wait_for_action(
                    task_run_id,
                    warning_count,
                    terminal_status=result.terminal_status,
                    completion_code=result.completion_code,
                    completion_summary=result.completion_summary,
                )
            if result.stop_pipeline:
                return self._succeed(
                    task_run_id,
                    warning_count,
                    completion_code=result.completion_code,
                    completion_summary=result.completion_summary,
                )

        return self._succeed(task_run_id, warning_count)

    def _claim(self, task_run_id: str) -> bool:
        """Claim queued work through a short compare-and-set-like transaction."""
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            repository = TaskRunRepository(unit.session)
            task_run = repository.get(task_run_id)
            if task_run is None or task_run.status != TaskRunStatus.QUEUED:
                return False
            if _is_expired(task_run.deadline_at, self._clock.now()):
                validate_task_run_transition(task_run.status, TaskRunStatus.TIMED_OUT)
                repository.update_status(
                    task_run,
                    TaskRunStatus.TIMED_OUT,
                    ended_at=self._clock.now(),
                    error_code="TASK_DEADLINE_EXCEEDED",
                    error_summary="task deadline elapsed before execution started",
                    retryable=False,
                )
                return False
            validate_task_run_transition(task_run.status, TaskRunStatus.RUNNING)
            now = self._clock.now()
            repository.update_status(
                task_run,
                TaskRunStatus.RUNNING,
                started_at=now,
                heartbeat_at=now,
                error_code=None,
                error_summary=None,
            )
            return True

    def _start_step(self, task_run_id: str, step_name: str, step_order: int) -> int:
        """Persist an in-progress step before invoking its external work."""
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            task_runs = TaskRunRepository(unit.session)
            task_run = task_runs.get(task_run_id)
            if task_run is None:
                msg = f"TaskRun disappeared before step {step_name}"
                raise RuntimeError(msg)
            task_runs.update_current_step(task_run, step_name)
            steps = TaskStepRepository(unit.session)
            task_step = steps.create(
                task_run_id=task_run_id,
                step_name=step_name,
                step_order=step_order,
                attempt=steps.next_attempt(task_run_id, step_name),
                status=TaskStepStatus.RUNNING,
                started_at=self._clock.now(),
                details_json="{}",
            )
            return task_step.id

    def _finish_step(self, task_step_id: int, result: StepResult) -> None:
        """Store the result of a completed checkpoint in its own short transaction."""
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            task_step = TaskStepRepository(unit.session).get(task_step_id)
            if task_step is None:
                msg = f"TaskStep {task_step_id} disappeared before completion"
                raise RuntimeError(msg)
            status = (
                TaskStepStatus.SUCCEEDED_WITH_WARNINGS
                if result.warning_count
                else TaskStepStatus.SUCCEEDED
            )
            TaskStepRepository(unit.session).finish(
                task_step,
                status=status,
                ended_at=self._clock.now(),
                input_count=result.input_count,
                output_count=result.output_count,
                warning_count=result.warning_count,
                input_fingerprint=result.input_fingerprint,
                output_fingerprint=result.output_fingerprint,
                checkpoint_json=result.checkpoint_json,
                details_json=result.details_json,
                artifact_path=result.artifact_path,
                retryable=result.retryable,
                llm_call_count=result.llm_call_count,
                llm_input_tokens=result.llm_input_tokens,
                llm_output_tokens=result.llm_output_tokens,
                tts_character_count=result.tts_character_count,
                cache_hit_count=result.cache_hit_count,
            )
            task_run = TaskRunRepository(unit.session).get(task_step.task_run_id)
            if task_run is None:
                msg = f"TaskRun {task_step.task_run_id} disappeared before usage persistence"
                raise RuntimeError(msg)
            TaskRunRepository(unit.session).add_usage(
                task_run,
                llm_call_count=result.llm_call_count,
                llm_input_tokens=result.llm_input_tokens,
                llm_output_tokens=result.llm_output_tokens,
                tts_character_count=result.tts_character_count,
                cache_hit_count=result.cache_hit_count,
            )

    def _fail_step_and_run(self, task_run_id: str, task_step_id: int, error: Exception) -> TaskRun:
        """Record the failed checkpoint and complete the run with a safe error summary."""
        summary = str(error)[:1000] or error.__class__.__name__
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            steps = TaskStepRepository(unit.session)
            task_step = steps.get(task_step_id)
            code, retryable = _classify_error(error)
            if task_step is not None:
                steps.finish(
                    task_step,
                    status=TaskStepStatus.FAILED,
                    ended_at=self._clock.now(),
                    error_code=code,
                    error_summary=summary,
                    retryable=retryable,
                )
            task_runs = TaskRunRepository(unit.session)
            task_run = task_runs.get(task_run_id)
            if task_run is None:
                msg = f"TaskRun {task_run_id} disappeared after step failure"
                raise RuntimeError(msg)
            validate_task_run_transition(task_run.status, TaskRunStatus.FAILED)
            return task_runs.update_status(
                task_run,
                TaskRunStatus.FAILED,
                ended_at=self._clock.now(),
                error_code=code,
                error_summary=summary,
                retryable=retryable,
            )

    def _fail_run_without_step(self, task_run_id: str, error: Exception) -> TaskRun:
        """Record a failed checkpoint restore without creating a misleading new step attempt."""
        summary = str(error)[:1000] or error.__class__.__name__
        code, retryable = _classify_error(error)
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            repository = TaskRunRepository(unit.session)
            task_run = repository.get(task_run_id)
            if task_run is None:
                raise RuntimeError(f"TaskRun {task_run_id} disappeared during checkpoint recovery")
            validate_task_run_transition(task_run.status, TaskRunStatus.FAILED)
            return repository.update_status(
                task_run,
                TaskRunStatus.FAILED,
                ended_at=self._clock.now(),
                error_code=code,
                error_summary=summary,
                retryable=retryable,
            )

    def _deadline_expired(self, task_run_id: str) -> bool:
        """Read the durable deadline at checkpoint boundaries without holding a long transaction."""
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            task_run = TaskRunRepository(unit.session).get(task_run_id)
            return task_run is not None and _is_expired(task_run.deadline_at, self._clock.now())

    def _time_out(self, task_run_id: str) -> TaskRun:
        """Finish an expired running task without pretending a content failure is retryable."""
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            repository = TaskRunRepository(unit.session)
            task_run = repository.get(task_run_id)
            if task_run is None:
                msg = f"TaskRun {task_run_id} disappeared during deadline enforcement"
                raise RuntimeError(msg)
            validate_task_run_transition(task_run.status, TaskRunStatus.TIMED_OUT)
            return repository.update_status(
                task_run,
                TaskRunStatus.TIMED_OUT,
                ended_at=self._clock.now(),
                error_code="TASK_DEADLINE_EXCEEDED",
                error_summary="task deadline elapsed at a pipeline checkpoint boundary",
                retryable=False,
            )

    def _restore_completed_checkpoints(
        self, context: PipelineContext
    ) -> tuple[int, frozenset[str]]:
        """Hydrate a recovery child from successful ancestor checkpoints before any repeat work."""
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            task_runs = TaskRunRepository(unit.session)
            current = task_runs.get(context.task_run_id)
            if current is None:
                msg = f"TaskRun {context.task_run_id} disappeared before checkpoint recovery"
                raise RuntimeError(msg)
            lineage = task_runs.lineage_ids(current)
            latest_attempts: dict[str, TaskStep] = {}
            lineage_position = {task_id: position for position, task_id in enumerate(lineage)}
            historical_steps = TaskStepRepository(unit.session).list_by_task_run_ids(lineage)
            for historical_step in sorted(
                historical_steps,
                key=lambda item: (
                    lineage_position[item.task_run_id],
                    item.step_order,
                    item.attempt,
                    item.id,
                ),
            ):
                latest_attempts[historical_step.step_name] = historical_step
            completed = {
                step_name: step
                for step_name, step in latest_attempts.items()
                if step.status
                in {
                    TaskStepStatus.SUCCEEDED,
                    TaskStepStatus.SUCCEEDED_WITH_WARNINGS,
                }
            }
        warning_count = 0
        completed_names: set[str] = set()
        for step in self._steps:
            persisted = completed.get(step.name)
            if persisted is None:
                break
            checkpoint = _validated_recovery_checkpoint(
                context, step, persisted, self._artifact_roots
            )
            if checkpoint is None:
                break
            restorer = getattr(step, "restore_checkpoint", None)
            try:
                if callable(restorer):
                    restored = restorer(context, checkpoint) is not False
                else:
                    restored = _restore_generic_checkpoint(context, step.name, checkpoint)
            except (KeyError, OSError, RuntimeError, TypeError, ValueError):
                restored = False
            if not restored:
                break
            warning_count += int(persisted.warning_count)
            completed_names.add(step.name)
        return warning_count, frozenset(completed_names)

    def _interrupt(self, task_run_id: str) -> TaskRun:
        """End a run after graceful shutdown requested no further checkpoints."""
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            task_runs = TaskRunRepository(unit.session)
            task_run = task_runs.get(task_run_id)
            if task_run is None:
                msg = f"TaskRun {task_run_id} disappeared during shutdown"
                raise RuntimeError(msg)
            validate_task_run_transition(task_run.status, TaskRunStatus.INTERRUPTED)
            return task_runs.update_status(
                task_run,
                TaskRunStatus.INTERRUPTED,
                ended_at=self._clock.now(),
                error_code="TASK_INTERRUPTED",
                error_summary="graceful shutdown stopped the next pipeline checkpoint",
                retryable=True,
            )

    def _succeed(
        self,
        task_run_id: str,
        warning_count: int,
        *,
        completion_code: str | None = None,
        completion_summary: str | None = None,
    ) -> TaskRun:
        """Commit the completed TaskRun after every checkpoint has succeeded."""
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            task_runs = TaskRunRepository(unit.session)
            task_run = task_runs.get(task_run_id)
            if task_run is None:
                msg = f"TaskRun {task_run_id} disappeared before success"
                raise RuntimeError(msg)
            status = (
                TaskRunStatus.SUCCEEDED_WITH_WARNINGS if warning_count else TaskRunStatus.SUCCEEDED
            )
            validate_task_run_transition(task_run.status, status)
            completed = task_runs.update_status(
                task_run,
                status,
                ended_at=self._clock.now(),
                warning_count=warning_count,
                error_code=completion_code,
                error_summary=completion_summary,
            )
            if (
                completed.task_type is TaskType.DAILY_GENERATE
                and completed.episode_id is not None
                and completed.started_at is not None
                and completed.ended_at is not None
            ):
                generation_seconds = max(
                    0,
                    round(
                        (
                            _as_utc(completed.ended_at) - _as_utc(completed.started_at)
                        ).total_seconds()
                    ),
                )
                episode = EpisodeRepository(unit.session).get(completed.episode_id)
                if episode is not None:
                    EpisodeRepository(unit.session).update_generation_metrics(
                        episode, generation_time_seconds=generation_seconds
                    )
            return completed

    def _wait_for_action(
        self,
        task_run_id: str,
        warning_count: int,
        *,
        terminal_status: TaskRunStatus,
        completion_code: str | None,
        completion_summary: str | None,
    ) -> TaskRun:
        """End a non-failed run when a checkpoint requires a human decision or revision."""
        if terminal_status is not TaskRunStatus.WAITING_ACTION:
            msg = f"unsupported non-failed terminal TaskRun status: {terminal_status.value}"
            raise ValueError(msg)
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            task_runs = TaskRunRepository(unit.session)
            task_run = task_runs.get(task_run_id)
            if task_run is None:
                msg = f"TaskRun {task_run_id} disappeared before waiting for human action"
                raise RuntimeError(msg)
            validate_task_run_transition(task_run.status, terminal_status)
            return task_runs.update_status(
                task_run,
                terminal_status,
                ended_at=self._clock.now(),
                warning_count=warning_count,
                retryable=False,
                error_code=completion_code or "WAITING_ACTION",
                error_summary=completion_summary or "pipeline stopped pending human action",
            )


def _is_expired(deadline_at: datetime | None, now: datetime) -> bool:
    """Compare SQLite's potentially naive timestamps against a UTC clock safely."""
    if deadline_at is None:
        return False
    deadline = deadline_at.replace(tzinfo=UTC) if deadline_at.tzinfo is None else deadline_at
    current = now.replace(tzinfo=UTC) if now.tzinfo is None else now
    return deadline <= current


def _as_utc(value: datetime) -> datetime:
    """Normalize SQLite's naive datetimes before calculating persisted wall-clock metrics."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _validated_recovery_checkpoint(
    context: PipelineContext,
    step: PipelineStep,
    persisted: TaskStep,
    artifact_roots: Sequence[Path],
) -> dict[str, object] | None:
    """Return a checkpoint only when its recorded output still proves safe to reuse."""
    if persisted.status not in {TaskStepStatus.SUCCEEDED, TaskStepStatus.SUCCEEDED_WITH_WARNINGS}:
        return None
    checkpoint = _checkpoint_mapping(persisted.checkpoint_json)
    if checkpoint is None:
        return None
    if persisted.artifact_path is not None and not _artifact_exists(
        persisted.artifact_path, artifact_roots
    ):
        return None
    expected_fingerprint = _expected_output_fingerprint(step, context, checkpoint)
    if expected_fingerprint is not None and expected_fingerprint != persisted.output_fingerprint:
        return None
    return checkpoint


def _checkpoint_mapping(raw: str | None) -> dict[str, object] | None:
    """Reject absent or malformed checkpoint JSON instead of treating it as reusable state."""
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _artifact_exists(artifact_path: str, artifact_roots: Sequence[Path]) -> bool:
    """Require a declared output to remain a regular file below an explicit controlled root."""
    relative = Path(artifact_path)
    if relative.is_absolute():
        return False
    for root in artifact_roots:
        candidate = (root / relative).resolve()
        if candidate.is_relative_to(root) and candidate.is_file():
            return True
    return False


def _expected_output_fingerprint(
    step: PipelineStep, context: PipelineContext, checkpoint: dict[str, object]
) -> str | None:
    """Ask steps to prove that a fingerprinted historical output is still current."""
    resolver = getattr(step, "expected_output_fingerprint", None)
    if not callable(resolver):
        return None
    try:
        fingerprint = resolver(context, checkpoint)
    except (KeyError, OSError, RuntimeError, TypeError, ValueError):
        return ""
    return fingerprint if isinstance(fingerprint, str) and fingerprint else ""


def _restore_generic_checkpoint(
    context: PipelineContext, step_name: str, checkpoint: dict[str, object]
) -> bool:
    """Restore the small durable IDs needed by deterministic built-in checkpoints."""
    key_map = {
        "collecting": ("article_ids", "collected_article_ids"),
        "filtering": ("eligible_article_ids", "eligible_article_ids"),
        "deduplicating": ("primary_article_ids", "deduplicated_article_ids"),
        "clustering": ("news_event_ids", "news_event_ids"),
        "ranking": ("selected_event_ids", "selected_news_event_ids"),
    }
    mapping = key_map.get(step_name)
    if mapping is None:
        return False
    source_key, target_key = mapping
    value = checkpoint.get(source_key)
    if isinstance(value, list) and all(isinstance(item, int) for item in value):
        context.values[target_key] = tuple(value)
        return True
    return False


def _classify_error(error: Exception) -> tuple[str, bool]:
    """Persist a specific stable code and conservative retryability for pipeline failures."""
    if isinstance(error, DailyCastError):
        return error.code, error.retryable
    if isinstance(error, TimeoutError | ConnectionError | OSError):
        return "PIPELINE_TRANSIENT_INFRASTRUCTURE_ERROR", True
    if isinstance(error, ValueError | LookupError | TypeError):
        return "PIPELINE_INPUT_INVALID", False
    return "PIPELINE_UNEXPECTED_ERROR", False


def build_collection_pipeline(
    collection_service: SourceCollectionService,
    article_service: ArticleService,
    extractor: ContentExtractor,
    news_processor: NewsProcessor,
    editorial_service: AIEditorialService,
    episode_service: EpisodeService,
    audio_service: AudioGenerationService,
    publication_service: PublicationService,
    budget_factory: Callable[[], BudgetController],
    *,
    data_dir: Path,
    auto_publish: bool = False,
    enforce_quality_gate: bool = True,
    collection_window_hours: int = 36,
    max_automatic_script_revisions: int = 1,
    clock: Clock | None = None,
) -> tuple[PipelineStep, ...]:
    """Build the review-gated collection-to-draft-audio sequence and publish checkpoint."""
    runtime_clock = clock or Clock()
    return cast(
        tuple[PipelineStep, ...],
        (
            CollectingStep(collection_service, collection_window_hours, runtime_clock),
            ExtractingStep(article_service, extractor),
            FilteringStep(news_processor),
            DeduplicatingStep(news_processor),
            ClusteringStep(news_processor),
            RankingStep(editorial_service, budget_factory),
            OutliningStep(editorial_service, data_dir, budget_factory),
            ScriptingStep(editorial_service, data_dir, budget_factory),
            CheckingStep(
                editorial_service,
                data_dir,
                budget_factory,
                max_automatic_script_revisions=max_automatic_script_revisions,
                enforce_quality_gate=enforce_quality_gate,
            ),
            CreateEpisodeStep(
                episode_service,
                data_dir,
                enforce_quality_gate=enforce_quality_gate,
            ),
            GenerateAudioStep(audio_service),
            PublishStep(episode_service, publication_service, auto_publish=auto_publish),
        ),
    )
