"""Sequential TaskRun orchestration with durable TaskStep checkpoints."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast

from sqlalchemy.orm import Session, sessionmaker

from dailycast.core.time import Clock
from dailycast.db.models import TaskRun, TaskRunStatus, TaskStepStatus
from dailycast.db.repositories import TaskRunRepository, TaskStepRepository
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
    ) -> None:
        self._session_factory = session_factory
        self._steps = tuple(steps)
        self._clock = clock or Clock()

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
        warning_count = 0
        for index, step in enumerate(self._steps, start=1):
            if stop_event.is_set():
                return self._interrupt(task_run_id)
            task_step_id = self._start_step(task_run_id, step.name, index)
            context.values["active_task_step_id"] = task_step_id
            try:
                result = await step.run(context)
            except Exception as error:
                return self._fail_step_and_run(task_run_id, task_step_id, error)
            self._finish_step(task_step_id, result)
            warning_count += result.warning_count

        return self._succeed(task_run_id, warning_count)

    def _claim(self, task_run_id: str) -> bool:
        """Claim queued work through a short compare-and-set-like transaction."""
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            repository = TaskRunRepository(unit.session)
            task_run = repository.get(task_run_id)
            if task_run is None or task_run.status != TaskRunStatus.QUEUED:
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
            task_step = TaskStepRepository(unit.session).create(
                task_run_id=task_run_id,
                step_name=step_name,
                step_order=step_order,
                attempt=1,
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
            )

    def _fail_step_and_run(self, task_run_id: str, task_step_id: int, error: Exception) -> TaskRun:
        """Record the failed checkpoint and complete the run with a safe error summary."""
        summary = str(error)[:1000] or error.__class__.__name__
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            steps = TaskStepRepository(unit.session)
            task_step = steps.get(task_step_id)
            if task_step is not None:
                steps.finish(
                    task_step,
                    status=TaskStepStatus.FAILED,
                    ended_at=self._clock.now(),
                    error_code="PIPELINE_STEP_FAILED",
                    error_summary=summary,
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
                error_code="PIPELINE_STEP_FAILED",
                error_summary=summary,
                retryable=True,
            )

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

    def _succeed(self, task_run_id: str, warning_count: int) -> TaskRun:
        """Commit the completed TaskRun after every checkpoint has succeeded."""
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            task_runs = TaskRunRepository(unit.session)
            task_run = task_runs.get(task_run_id)
            if task_run is None:
                msg = f"TaskRun {task_run_id} disappeared before success"
                raise RuntimeError(msg)
            validate_task_run_transition(task_run.status, TaskRunStatus.SUCCEEDED)
            return task_runs.update_status(
                task_run,
                TaskRunStatus.SUCCEEDED,
                ended_at=self._clock.now(),
                warning_count=warning_count,
            )


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
            ),
            CreateEpisodeStep(episode_service, data_dir),
            GenerateAudioStep(audio_service),
            PublishStep(publication_service, auto_publish=auto_publish),
        ),
    )
