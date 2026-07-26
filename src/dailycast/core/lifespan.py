"""FastAPI lifespan that initializes the currently implemented runtime infrastructure."""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from dailycast.core.config import Settings, load_settings
from dailycast.core.logging import configure_logging
from dailycast.db.models import SourceKind, TaskType, TriggerType
from dailycast.db.revision import RevisionStatus, inspect_revision
from dailycast.db.session import create_session_factory, create_sqlite_engine
from dailycast.episodes.service import EpisodeService
from dailycast.llm.budget import BudgetController
from dailycast.llm.contracts import LLMProvider
from dailycast.llm.editorial_service import AIEditorialService
from dailycast.llm.providers.openai_compatible import OpenAICompatibleLLMProvider
from dailycast.llm.providers.openai_responses import OpenAIResponsesLLMProvider
from dailycast.news.service import NewsProcessor
from dailycast.news.types import ProcessingPolicy
from dailycast.pipeline.contracts import TaskCommand
from dailycast.pipeline.executor import InProcessTaskExecutor
from dailycast.pipeline.orchestrator import PipelineOrchestrator, build_collection_pipeline
from dailycast.pipeline.recovery import RecoveryService
from dailycast.pipeline.submission import TaskSubmissionService
from dailycast.publishing.rss import RSSPublisher, RSSSettings
from dailycast.publishing.service import PublicationService
from dailycast.scheduler.service import SchedulerService
from dailycast.sources.bootstrap import seed_missing_sources
from dailycast.sources.extraction import ContentExtractor, SafeHttpFetcher
from dailycast.sources.html_list import HTMLListCollector
from dailycast.sources.rss import RSSCollector
from dailycast.sources.service import ArticleService, SourceCollectionService
from dailycast.tts.merge import FFmpegMerger
from dailycast.tts.preprocess import PronunciationDictionary, TTSPreprocessor
from dailycast.tts.providers.edge import EdgeTTSProvider
from dailycast.tts.service import AudioGenerationService, TTSGenerationSettings

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class AppRuntime:
    """Runtime resources shared through FastAPI dependency injection."""

    settings: Settings
    engine: Engine
    session_factory: sessionmaker[Session]
    startup_revision_status: RevisionStatus | None
    startup_revision_error: str | None
    executor: InProcessTaskExecutor | None
    submission_service: TaskSubmissionService | None
    scheduler: SchedulerService | None


def build_lifespan(
    config_path: Path | None,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """Build an app lifespan bound to an optional configuration path for tests."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings = load_settings(config_path=config_path)
        configure_logging(settings.logging.level)
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        settings.public_dir.mkdir(parents=True, exist_ok=True)
        engine = create_sqlite_engine(settings.database)
        alembic_ini_path = PROJECT_ROOT / "alembic.ini"
        try:
            startup_revision_status = inspect_revision(
                engine=engine,
                ini_path=alembic_ini_path,
                database_url=settings.database.url,
            )
            startup_revision_error = None
        except Exception as error:  # A missing/malformed revision must make readyz fail closed.
            startup_revision_status = None
            startup_revision_error = str(error)
        session_factory = create_session_factory(engine)
        executor: InProcessTaskExecutor | None = None
        submission_service: TaskSubmissionService | None = None
        scheduler: SchedulerService | None = None
        llm_client: httpx.AsyncClient | None = None
        if startup_revision_status is not None and startup_revision_status.is_current:
            created_source_count = seed_missing_sources(
                session_factory,
                settings.resolve_path(settings.sources.config_path),
            )
            logger.info(
                "source_seed_completed", extra={"created_source_count": created_source_count}
            )
            fetcher = SafeHttpFetcher()
            article_service = ArticleService(session_factory)
            collection_service = SourceCollectionService(
                session_factory,
                {
                    SourceKind.RSS: RSSCollector(fetcher),
                    SourceKind.HTML_LIST: HTMLListCollector(fetcher),
                },
                article_service,
            )
            processing_policy = ProcessingPolicy(
                max_age_hours=settings.processing.max_age_hours,
                min_content_length=settings.processing.min_content_length,
                similarity_threshold=settings.processing.similarity_threshold,
            )
            news_processor = NewsProcessor(
                session_factory,
                processing_policy,
                timezone=settings.app.timezone,
            )
            llm_client = httpx.AsyncClient()
            llm_provider = build_llm_provider(settings, http_client=llm_client)
            editorial_service = AIEditorialService(
                session_factory,
                llm_provider,
                max_candidates=settings.editorial.max_candidates,
                max_selected_events=settings.editorial.max_selected_events,
                max_ai_events=settings.editorial.max_ai_events,
                min_domestic_events_when_available=(
                    settings.editorial.min_domestic_events_when_available
                ),
                max_sources_per_event=settings.editorial.max_sources_per_event,
                max_chars_per_source=settings.editorial.max_chars_per_source,
                max_total_evidence_chars=settings.editorial.max_total_evidence_chars,
                min_publishable_events=settings.editorial.min_publishable_events,
                target_duration_seconds=settings.editorial.target_duration_seconds,
                duration_tolerance_seconds=settings.editorial.outline_duration_tolerance_seconds,
                max_outline_sections=settings.editorial.max_outline_sections,
                estimated_chars_per_second=settings.editorial.estimated_chars_per_second,
                script_duration_tolerance_ratio=(
                    settings.editorial.script_duration_tolerance_ratio
                ),
                max_script_chars=settings.editorial.max_script_chars,
                max_section_chars=settings.editorial.max_section_chars,
            )
            if settings.tts.provider != "edge_tts":
                raise RuntimeError("unsupported configured TTS provider")
            audio_service = AudioGenerationService(
                session_factory,
                EdgeTTSProvider(
                    timeout_seconds=settings.tts.timeout_seconds,
                    max_retries=settings.tts.max_retries,
                ),
                data_dir=settings.data_dir,
                merger=FFmpegMerger(
                    sample_rate=settings.ffmpeg.sample_rate,
                    bitrate=settings.ffmpeg.bitrate,
                ),
                settings=TTSGenerationSettings(
                    voice=settings.tts.voice,
                    speed=settings.tts.speed,
                    format=settings.tts.format,
                    text_mode=settings.tts.text_mode,
                    opening_summary_speed=settings.tts.opening_summary_speed,
                    closing_summary_speed=settings.tts.closing_summary_speed,
                    cache_enabled=settings.tts.cache_enabled,
                ),
                preprocessor=TTSPreprocessor(
                    dictionary=PronunciationDictionary.from_yaml(
                        settings.resolve_path(settings.tts.pronunciation_dictionary_path)
                    ),
                    text_mode=settings.tts.text_mode,
                ),
            )
            publication_service = PublicationService(
                session_factory,
                RSSPublisher(
                    data_dir=settings.data_dir,
                    public_dir=settings.public_dir,
                    settings=RSSSettings(
                        public_base_url=settings.publishing.public_base_url,
                        feed_title=settings.publishing.feed_title,
                        feed_description=settings.publishing.feed_description,
                        language=settings.publishing.language,
                        author=settings.publishing.author,
                    ),
                ),
            )
            orchestrator = PipelineOrchestrator(
                session_factory,
                build_collection_pipeline(
                    collection_service,
                    article_service,
                    ContentExtractor(fetcher),
                    news_processor,
                    editorial_service,
                    EpisodeService(session_factory),
                    audio_service,
                    publication_service,
                    lambda: BudgetController(
                        max_calls=settings.llm.budget.max_calls,
                        max_input_tokens=settings.llm.budget.max_input_tokens,
                    ),
                    data_dir=settings.data_dir,
                    auto_publish=settings.publishing.auto_publish,
                    enforce_quality_gate=settings.editorial.enforce_quality_gate,
                    max_automatic_script_revisions=(
                        settings.editorial.max_automatic_script_revisions
                    ),
                ),
                artifact_roots=(settings.data_dir, settings.public_dir),
            )
            executor = InProcessTaskExecutor(session_factory, orchestrator)
            publication_service.reconcile()
            submission_service = TaskSubmissionService(session_factory, executor)
            await executor.start()
            await RecoveryService(session_factory, submission_service).recover()
            scheduler = SchedulerService(
                submission_service,
                lambda: build_daily_generation_command(
                    settings, trigger_type=TriggerType.SCHEDULED
                ),
                cron_expression=settings.scheduler.cron_expression,
                timezone=settings.app.timezone,
                enabled=settings.scheduler.enabled,
            )
            try:
                scheduler.start()
            except Exception:
                # Scheduler availability must not turn an otherwise ready local product into a
                # startup outage. The next restart can safely retry this in-process capability.
                logger.exception(
                    "scheduler failed to start; application will continue without ticks"
                )
                scheduler = None
        runtime = AppRuntime(
            settings=settings,
            engine=engine,
            session_factory=session_factory,
            startup_revision_status=startup_revision_status,
            startup_revision_error=startup_revision_error,
            executor=executor,
            submission_service=submission_service,
            scheduler=scheduler,
        )
        app.state.runtime = runtime
        logger.info("application_started")
        try:
            yield
        finally:
            if scheduler is not None:
                scheduler.shutdown()
            if executor is not None:
                await executor.shutdown(grace_seconds=30)
            if llm_client is not None:
                await llm_client.aclose()
            engine.dispose()
            logger.info("application_stopped")

    return lifespan


def build_llm_provider(settings: Settings, *, http_client: httpx.AsyncClient) -> LLMProvider:
    """Select the configured provider without leaking protocol details into editorial flow."""
    if settings.llm.provider == "openai_compatible":
        return OpenAICompatibleLLMProvider(
            base_url=settings.llm.base_url,
            api_key=settings.llm.api_key,
            model=settings.llm.model,
            timeout_seconds=settings.llm.timeout_seconds,
            temperature=settings.llm.temperature,
            top_p=settings.llm.top_p,
            max_retries=settings.llm.max_retries,
            response_format=settings.llm.response_format,
            http_client=http_client,
        )
    if settings.llm.provider == "openai_responses":
        return OpenAIResponsesLLMProvider(
            base_url=settings.llm.base_url,
            api_key=settings.llm.api_key,
            model=settings.llm.model,
            timeout_seconds=settings.llm.timeout_seconds,
            temperature=settings.llm.temperature,
            top_p=settings.llm.top_p,
            max_retries=settings.llm.max_retries,
            response_format=settings.llm.response_format,
            http_client=http_client,
        )
    msg = f"unsupported configured LLM provider: {settings.llm.provider}"
    raise RuntimeError(msg)


def build_daily_generation_command(settings: Settings, *, trigger_type: TriggerType) -> TaskCommand:
    """Build the one documented daily command for scheduler and manual submission alike."""
    now = datetime.now(ZoneInfo(settings.app.timezone))
    episode_date = now.date().isoformat()
    scheduled_idempotency_key = (
        f"scheduled:daily:{episode_date}:rss-v1" if trigger_type is TriggerType.SCHEDULED else None
    )
    return TaskCommand(
        task_type=TaskType.DAILY_GENERATE,
        request={
            "edition": "daily",
            "episode_date": episode_date,
        },
        config_snapshot={
            "pipeline": "rss-v1",
            "processing": settings.processing.model_dump(mode="json"),
            "editorial": settings.editorial.model_dump(mode="json"),
            "ffmpeg": settings.ffmpeg.model_dump(mode="json"),
            "tts": settings.tts.model_dump(mode="json"),
            "publishing": settings.publishing.model_dump(mode="json"),
        },
        pipeline_version="rss-v1",
        trigger_type=trigger_type,
        idempotency_key=scheduled_idempotency_key,
        deadline_at=now + timedelta(seconds=settings.task_execution.deadline_seconds),
    )
