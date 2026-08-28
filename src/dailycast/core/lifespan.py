"""FastAPI lifespan that initializes the currently implemented runtime infrastructure."""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from dailycast.briefing.alerts import BriefingAlertReporter
from dailycast.briefing.monitoring import BriefingProviderProbe
from dailycast.briefing.scheduler import BriefingScheduler
from dailycast.briefing.selection import load_selection_policy
from dailycast.briefing.service import BriefingService
from dailycast.briefing.webhook import WebhookNotifier
from dailycast.core.config import LLMProviderSettings, Settings, load_settings
from dailycast.core.logging import configure_logging
from dailycast.db.models import SourceKind, TaskType, TriggerType
from dailycast.db.revision import RevisionStatus, inspect_revision
from dailycast.db.session import create_session_factory, create_sqlite_engine
from dailycast.episodes.service import EpisodeService
from dailycast.llm.budget import BudgetController
from dailycast.llm.contracts import LLMProvider, WebResearchProvider
from dailycast.llm.editorial_service import AIEditorialService
from dailycast.llm.providers.failover import FailoverLLMProvider
from dailycast.llm.providers.openai_compatible import OpenAICompatibleLLMProvider
from dailycast.llm.providers.openai_responses import OpenAIResponsesLLMProvider
from dailycast.news.service import NewsProcessor
from dailycast.news.types import ProcessingPolicy
from dailycast.pipeline.contracts import TaskCommand
from dailycast.pipeline.executor import InProcessTaskExecutor
from dailycast.pipeline.orchestrator import PipelineOrchestrator, build_collection_pipeline
from dailycast.pipeline.recovery import RecoveryService
from dailycast.pipeline.submission import TaskSubmissionService
from dailycast.publishing.contracts import Publisher
from dailycast.publishing.dispatcher import PublicationDispatcher, RSSDistributionPublisher
from dailycast.publishing.netease import (
    NetEasePlaywrightPublisher,
)
from dailycast.publishing.netease import (
    NetEasePublishingSettings as NetEasePublisherSettings,
)
from dailycast.publishing.rss import RSSPublisher, RSSSettings
from dailycast.publishing.service import PublicationService
from dailycast.publishing.xiaoyuzhou import XiaoyuzhouPublisher
from dailycast.scheduler.service import SchedulerService
from dailycast.sources.bootstrap import (
    load_configured_source_ids,
    seed_missing_sources,
    sync_configured_sources,
)
from dailycast.sources.extraction import ContentExtractor, SafeHttpFetcher
from dailycast.sources.html_list import HTMLListCollector
from dailycast.sources.research import ResearchCollector, UnavailableWebResearchProvider
from dailycast.sources.rss import RSSCollector
from dailycast.sources.service import ArticleService, SourceCollectionService
from dailycast.tts.merge import FFmpegMerger
from dailycast.tts.preprocess import PronunciationDictionary, TTSPreprocessor
from dailycast.tts.providers.edge import EdgeTTSProvider
from dailycast.tts.service import AudioGenerationService, TTSGenerationSettings

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[3]


def build_distribution_publishers(
    settings: Settings, publication_service: PublicationService
) -> tuple[Publisher, ...]:
    """Build enabled target adapters with browser state rooted in private DATA_DIR."""
    publishers: list[Publisher] = []
    if settings.publishing.rss.enabled:
        publishers.append(RSSDistributionPublisher(publication_service))
    if settings.publishing.netease.enabled:
        configured_profile_dir = settings.publishing.netease.profile_dir
        profile_dir = (
            configured_profile_dir
            if configured_profile_dir.is_absolute()
            else settings.data_dir / configured_profile_dir
        )
        configured_cover_path = settings.publishing.netease.cover_path
        cover_path = (
            settings.resolve_path(configured_cover_path)
            if configured_cover_path is not None
            else None
        )
        netease_settings = settings.publishing.netease
        publishers.append(
            NetEasePlaywrightPublisher(
                NetEasePublisherSettings(
                    creator_url=netease_settings.creator_url,
                    profile_dir=profile_dir,
                    headless=netease_settings.headless,
                    cover_path=cover_path,
                    category=netease_settings.category,
                )
            )
        )
    if settings.publishing.xiaoyuzhou.enabled:
        publishers.append(XiaoyuzhouPublisher())
    return tuple(publishers)


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
    briefing_service: BriefingService | None = None
    briefing_scheduler: BriefingScheduler | None = None
    publication_dispatcher: PublicationDispatcher | None = None


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
        briefing_service: BriefingService | None = None
        briefing_scheduler: BriefingScheduler | None = None
        publication_dispatcher: PublicationDispatcher | None = None
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
            processing_policy = ProcessingPolicy(
                max_age_hours=settings.processing.max_age_hours,
                source_max_age_hours=settings.processing.source_max_age_hours,
                min_content_length=settings.processing.min_content_length,
                similarity_threshold=settings.processing.similarity_threshold,
            )
            news_processor = NewsProcessor(
                session_factory,
                processing_policy,
                timezone=settings.app.timezone,
            )
            llm_client = httpx.AsyncClient()
            primary_llm_provider = _build_direct_llm_provider(settings.llm, http_client=llm_client)
            llm_provider = build_llm_provider(
                settings,
                http_client=llm_client,
                primary_provider=primary_llm_provider,
            )
            collection_service = SourceCollectionService(
                session_factory,
                {
                    SourceKind.RSS: RSSCollector(
                        fetcher, rsshub_base_url=settings.briefing.rsshub_base_url
                    ),
                    SourceKind.HTML_LIST: HTMLListCollector(fetcher),
                    SourceKind.WEB_RESEARCH: ResearchCollector(
                        build_web_research_provider(primary_llm_provider),
                        ContentExtractor(fetcher),
                        settings.web_research,
                    ),
                },
                article_service,
                source_max_age_hours=settings.processing.source_max_age_hours,
            )
            editorial_service = AIEditorialService(
                session_factory,
                llm_provider,
                max_candidates=settings.editorial.max_candidates,
                max_selected_events=settings.editorial.max_selected_events,
                max_ai_events=settings.editorial.max_ai_events,
                min_domestic_events_when_available=(
                    settings.editorial.min_domestic_events_when_available
                ),
                min_recruitment_events_when_available=(
                    settings.editorial.min_recruitment_events_when_available
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
            publication_dispatcher = PublicationDispatcher(
                session_factory,
                build_distribution_publishers(settings, publication_service),
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
                    publication_dispatcher,
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
            # The RSS service stays the source of truth even when no target row is
            # mid-publish: verifying published feed items on every startup keeps a
            # lost or recreated public volume self-healing before the next episode.
            publication_service.reconcile()
            await publication_dispatcher.reconcile()
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
            if settings.briefing.enabled:
                briefing_service, briefing_scheduler = _build_briefing_runtime(
                    settings,
                    session_factory,
                    collection_service,
                    article_service,
                    fetcher,
                    news_processor,
                    llm_provider,
                )
        runtime = AppRuntime(
            settings=settings,
            engine=engine,
            session_factory=session_factory,
            startup_revision_status=startup_revision_status,
            startup_revision_error=startup_revision_error,
            executor=executor,
            submission_service=submission_service,
            scheduler=scheduler,
            briefing_service=briefing_service,
            briefing_scheduler=briefing_scheduler,
            publication_dispatcher=publication_dispatcher,
        )
        app.state.runtime = runtime
        logger.info("application_started")
        try:
            yield
        finally:
            if briefing_scheduler is not None:
                briefing_scheduler.shutdown()
            if scheduler is not None:
                scheduler.shutdown()
            if executor is not None:
                await executor.shutdown(grace_seconds=30)
            if llm_client is not None:
                await llm_client.aclose()
            engine.dispose()
            logger.info("application_stopped")

    return lifespan


def _build_briefing_runtime(
    settings: Settings,
    session_factory: sessionmaker[Session],
    collection_service: SourceCollectionService,
    article_service: ArticleService,
    fetcher: SafeHttpFetcher,
    news_processor: NewsProcessor,
    llm_provider: LLMProvider,
) -> tuple[BriefingService, BriefingScheduler | None]:
    """Build the independent briefing flow, reusing the podcast's collectors and LLM."""
    selection_policy = load_selection_policy(
        settings.resolve_path(settings.briefing.selection_policy_path)
    )
    source_config_path = settings.resolve_path(settings.briefing.sources_config_path)
    source_sync = sync_configured_sources(session_factory, source_config_path)
    logger.info(
        "briefing_source_sync_completed",
        extra={
            "created_source_count": source_sync.created_count,
            "updated_source_count": source_sync.updated_count,
            "unchanged_source_count": source_sync.unchanged_count,
        },
    )
    notifier: WebhookNotifier | None = None
    if settings.briefing.webhook_enabled:
        assert settings.briefing.webhook_url is not None
        notifier = WebhookNotifier(
            settings.briefing.webhook_url,
            payload_format=settings.briefing.webhook_format,
        )
    alert_reporter: BriefingAlertReporter | None = None
    if settings.monitoring.webhook_url:
        alert_reporter = BriefingAlertReporter(
            WebhookNotifier(
                settings.monitoring.webhook_url,
                payload_format=settings.monitoring.webhook_format,
            ),
            now=lambda: datetime.now(UTC),
            timezone=settings.app.timezone,
        )

    async def report_alert(stage: str, error: Exception, briefing_date: str | None) -> None:
        if alert_reporter is None:
            return
        await alert_reporter.report(stage=stage, error=error, briefing_date=briefing_date)

    provider_preflight = None
    if alert_reporter is not None:
        configured_providers = getattr(llm_provider, "providers", (llm_provider,))
        provider_preflight = BriefingProviderProbe(
            configured_providers, alert=report_alert
        ).run
    briefing_service = BriefingService(
        session_factory,
        collection_service,
        article_service,
        ContentExtractor(fetcher),
        news_processor,
        llm_provider,
        notifier,
        alert_reporter=alert_reporter,
        window_hours=settings.briefing.window_hours,
        max_items_per_category=settings.briefing.max_items_per_category,
        max_evidence_chars_per_article=settings.briefing.max_evidence_chars_per_article,
        output_dir=settings.data_dir / "work" / "briefings",
        budget_factory=lambda: BudgetController(
            max_calls=settings.llm.budget.max_calls,
            max_input_tokens=settings.llm.budget.max_input_tokens,
            max_output_tokens=settings.llm.budget.max_output_tokens,
        ),
        briefing_source_ids=load_configured_source_ids(source_config_path),
        selection_policy=selection_policy,
        timezone=settings.app.timezone,
    )
    briefing_scheduler = BriefingScheduler(
        briefing_service.prepare,
        briefing_service.deliver_prepared,
        preparation_cron_expression=settings.briefing.preparation_cron_expression,
        preparation_retry_cron_expression=settings.briefing.preparation_retry_cron_expression,
        delivery_cron_expression=settings.briefing.cron_expression,
        timezone=settings.app.timezone,
        alert=report_alert if alert_reporter is not None else None,
        provider_preflight=provider_preflight,
        provider_preflight_cron_expression=(
            settings.monitoring.provider_preflight_cron_expression
            if provider_preflight is not None
            else None
        ),
    )
    try:
        briefing_scheduler.start()
    except Exception:
        # Like the podcast scheduler, a briefing tick failure must not become a startup outage.
        logger.exception(
            "briefing scheduler failed to start; application will continue without ticks"
        )
        return briefing_service, None
    return briefing_service, briefing_scheduler


def build_llm_provider(
    settings: Settings,
    *,
    http_client: httpx.AsyncClient,
    primary_provider: LLMProvider | None = None,
) -> LLMProvider:
    """Build the preferred provider and its optional ordered fallback."""
    primary = primary_provider or _build_direct_llm_provider(settings.llm, http_client=http_client)
    if settings.llm.fallback is None:
        return primary
    fallback = _build_direct_llm_provider(settings.llm.fallback, http_client=http_client)
    return FailoverLLMProvider(primary, fallback)


def build_web_research_provider(primary_provider: LLMProvider) -> WebResearchProvider:
    """Expose native discovery only from the configured primary Responses provider."""
    if isinstance(primary_provider, OpenAIResponsesLLMProvider):
        return primary_provider
    return UnavailableWebResearchProvider()


def _build_direct_llm_provider(
    provider_settings: LLMProviderSettings,
    *,
    http_client: httpx.AsyncClient,
) -> LLMProvider:
    """Select one wire adapter without leaking protocol details into editorial flow."""
    if provider_settings.provider == "openai_compatible":
        return OpenAICompatibleLLMProvider(
            base_url=provider_settings.base_url,
            api_key=provider_settings.api_key,
            model=provider_settings.model,
            timeout_seconds=provider_settings.timeout_seconds,
            temperature=provider_settings.temperature,
            top_p=provider_settings.top_p,
            max_output_tokens=provider_settings.max_output_tokens,
            max_retries=provider_settings.max_retries,
            response_format=provider_settings.response_format,
            http_client=http_client,
        )
    if provider_settings.provider == "openai_responses":
        return OpenAIResponsesLLMProvider(
            base_url=provider_settings.base_url,
            api_key=provider_settings.api_key,
            model=provider_settings.model,
            timeout_seconds=provider_settings.timeout_seconds,
            temperature=provider_settings.temperature,
            top_p=provider_settings.top_p,
            max_output_tokens=provider_settings.max_output_tokens,
            max_retries=provider_settings.max_retries,
            response_format=provider_settings.response_format,
            http_client=http_client,
        )
    msg = f"unsupported configured LLM provider: {provider_settings.provider}"
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
