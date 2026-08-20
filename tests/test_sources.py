"""Sprint 3A RSS collection, extraction, Article persistence, and pipeline tests."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic import command
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dailycast.core.config import load_settings
from dailycast.core.time import Clock
from dailycast.db.models import (
    Article,
    ArticleStatus,
    Episode,
    EpisodeStatus,
    LLMOperation,
    NewsEvent,
    Source,
    SourceKind,
    TaskRunStatus,
    TaskType,
)
from dailycast.db.repositories import ArticleRepository, SourceRepository, TaskRunRepository
from dailycast.db.revision import build_alembic_config
from dailycast.db.session import create_session_factory, create_sqlite_engine
from dailycast.db.transactions import UnitOfWork
from dailycast.episodes.service import EpisodeService
from dailycast.llm.budget import BudgetController
from dailycast.llm.contracts import LLMMessage, LLMUsage, StructuredResult
from dailycast.llm.editorial_service import AIEditorialService
from dailycast.news.normalization import normalize_title, title_hash
from dailycast.news.service import NewsProcessor
from dailycast.news.types import ProcessingPolicy
from dailycast.pipeline.contracts import TaskCommand
from dailycast.pipeline.executor import InProcessTaskExecutor
from dailycast.pipeline.orchestrator import PipelineOrchestrator, build_collection_pipeline
from dailycast.pipeline.submission import TaskSubmissionService
from dailycast.sources.contracts import ArticleCandidate, CollectionResult, CollectionWindow
from dailycast.sources.extraction import ContentExtractor, FetchPolicy, SafeHttpFetcher
from dailycast.sources.html_list import HTMLListCollector
from dailycast.sources.rss import RSSCollector
from dailycast.sources.service import (
    ArticleService,
    ArticleValidationError,
    SourceCollectionService,
)
from dailycast.tts.service import AudioGenerationResult


class FakePublicationDispatcher:
    """Test double kept unused when review-gated auto publication is disabled for the daily flow."""

    async def publish(self, episode_id: int) -> object:
        """Fail loudly if a collection test bypasses the review-gated configuration."""
        raise AssertionError(f"unexpected auto publish for Episode {episode_id}")


class FixedClock(Clock):
    """Keep collection-window integration fixtures independent of the wall-clock date."""

    def __init__(self, value: datetime) -> None:
        self._value = value

    def now(self) -> datetime:
        """Return the fixture instant in UTC."""
        return self._value


class FakeRankingProvider:
    """Test-only score provider that rates each bounded EventCard without network I/O."""

    provider_name = "fake"
    model = "fake-ranking-model"
    max_output_tokens = 100

    def generation_config_hash(self, model_options: dict[str, object]) -> str:
        """Return one stable semantic identity for this deterministic test provider."""
        del model_options
        return "a" * 64

    async def generate_structured(
        self,
        operation: LLMOperation,
        messages: tuple[LLMMessage, ...],
        response_schema: type[BaseModel],
        model_options: dict[str, object],
    ) -> StructuredResult:
        """Return valid score and outline payloads without network I/O."""
        del response_schema, model_options
        payload = json.loads(messages[-1].content)
        if operation is LLMOperation.SCORE_EVENTS:
            content: dict[str, object] = {
                "scores": [
                    {
                        "event_id": event["event_id"],
                        "importance": 80,
                        "relevance": 80,
                        "confidence": 80,
                        "recommend": True,
                        "reason": "Fixture event is relevant",
                        "risks": [],
                    }
                    for event in payload["events"]
                ]
            }
        elif operation is LLMOperation.GENERATE_OUTLINE:
            event_ids = [event["event_id"] for event in payload["events"]]
            target_seconds = payload["constraints"]["target_duration_seconds"]
            news_seconds = target_seconds - 60
            content = {
                "schema_version": "1",
                "title_angle": "Fixture evidence-first outline",
                "target_seconds": target_seconds,
                "sections": [
                    {
                        "section_id": "intro",
                        "type": "intro",
                        "event_ids": [],
                        "goal": "Frame the briefing.",
                        "key_facts": [],
                        "seconds": 30,
                    },
                    {
                        "section_id": "news-1",
                        "type": "news",
                        "event_ids": event_ids,
                        "goal": "Explain the selected event evidence.",
                        "key_facts": ["Use the supplied evidence."],
                        "seconds": news_seconds,
                    },
                    {
                        "section_id": "outro",
                        "type": "outro",
                        "event_ids": [],
                        "goal": "Close the briefing.",
                        "key_facts": [],
                        "seconds": 30,
                    },
                ],
            }
        elif operation is LLMOperation.GENERATE_SCRIPT:
            outline = payload["outline"]
            dossiers_by_event = {
                dossier["event_id"]: dossier for dossier in payload["evidence_dossiers"]
            }
            text_repeats = max(1, int(outline["target_seconds"] * 4 / len(outline["sections"]) / 6))
            sections = []
            for section in outline["sections"]:
                event_ids = section["event_ids"]
                article_ids = [
                    dossiers_by_event[event_id]["representative_article"]["article_id"]
                    for event_id in event_ids
                ]
                is_news = section["type"] == "news"
                sections.append(
                    {
                        "section_id": section["section_id"],
                        "text": ("新闻测试内容" if is_news else "节目衔接内容") * text_repeats,
                        "event_ids": event_ids,
                        "article_ids": article_ids,
                        "claims": (
                            [{"text": "使用提供证据。", "article_ids": article_ids}]
                            if is_news
                            else []
                        ),
                    }
                )
            content = {"schema_version": "1", "sections": sections, "pronunciation_hints": []}
        elif operation is LLMOperation.REVIEW_SCRIPT:
            content = {
                "schema_version": "1",
                "verdict": "pass",
                "issues": [],
                "suggested_changes": [],
            }
        elif operation is LLMOperation.GENERATE_METADATA:
            content = {
                "schema_version": "1",
                "title": "Fixture DailyCast",
                "description": "Fixture metadata from the validated script.",
                "keywords": ["fixture", "news"],
            }
        else:
            raise AssertionError(f"unexpected fixture operation: {operation}")
        return StructuredResult(
            content=content,
            model=self.model,
            usage=LLMUsage(input_tokens=1, output_tokens=1),
            request_id=f"fixture-{operation.value}",
        )


class AllowAllUrls:
    """Test-only URL validator used with deterministic in-memory HTTP transports."""

    def validate(self, url: str) -> None:
        """Accept fixture URLs without doing external DNS resolution."""
        del url


class FakeAudioGenerationService:
    """Test-double checkpoint boundary for deterministic collection integration."""

    async def generate_episode_draft(self, episode_id: int) -> AudioGenerationResult:
        """Return a completed draft-audio result; detailed TTS behavior has dedicated tests."""
        return AudioGenerationResult(
            episode_id=episode_id,
            segment_count=3,
            cache_hits=0,
            provider_calls=3,
            duration_ms=3000,
            audio_version=1,
            draft_audio_path=f"audio/{episode_id}.mp3",
            tts_character_count=0,
        )


def upgraded_factory(app_config_path: Path) -> tuple[Any, sessionmaker[Session]]:
    """Create an empty V1 database through the Alembic migration path."""
    settings = load_settings(config_path=app_config_path)
    ini_path = Path(__file__).resolve().parents[1] / "alembic.ini"
    command.upgrade(
        build_alembic_config(ini_path=ini_path, database_url=settings.database.url), "head"
    )
    engine = create_sqlite_engine(settings.database)
    return engine, create_session_factory(engine)


def source_values() -> dict[str, object]:
    """Return persisted Source values without leaking SQLAlchemy instance state."""
    timestamp = datetime.now(UTC)
    return {
        "id": "hacker-news-rss",
        "name": "Hacker News",
        "kind": SourceKind.RSS,
        "entry_url": "https://feed.example.test/rss",
        "normalized_entry_url": "https://feed.example.test/rss",
        "config_json": "{}",
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def source() -> Source:
    """Build the minimum enabled RSS source used by collection tests."""
    return Source(**source_values())


def fixture_feed() -> bytes:
    """Load the static RSS document without contacting a public service."""
    return (Path(__file__).parent / "fixtures" / "feeds" / "hacker-news.xml").read_bytes()


def test_rss_collector_returns_article_candidates_from_fixture() -> None:
    """RSS discovery maps title, URL, summary, publication time, and GUID into DTOs."""

    async def scenario() -> None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"content-type": "application/rss+xml"},
                    content=fixture_feed(),
                    request=request,
                )
            )
        )
        try:
            collector = RSSCollector(SafeHttpFetcher(client, url_validator=AllowAllUrls()))
            result = await collector.collect(
                source(),
                CollectionWindow(
                    start=datetime(2026, 7, 21, 0, 0, tzinfo=UTC),
                    end=datetime(2026, 7, 22, 0, 0, tzinfo=UTC),
                ),
            )
        finally:
            await client.aclose()

        assert result.error is None
        assert len(result.candidates) == 2
        first = result.candidates[0]
        assert first.title == "First collection candidate"
        assert first.url == "https://article.example.test/first?utm_source=rss"
        assert first.summary == "First summary."
        assert first.external_id == "hn-1001"
        assert first.published_at == datetime(2026, 7, 21, 10, 0, tzinfo=UTC)

    asyncio.run(scenario())


def test_html_list_collector_discovers_only_matching_public_recruitment_announcements() -> None:
    """Official list pages become dated candidates after their recruitment title is matched."""

    async def scenario() -> None:
        html = """
        <html><body><ul>
        <li>
          <a href="/content/show?id=100">2026年常州市事业单位公开招聘工作人员公告</a>
          <span>2026-07-16</span>
        </li>
        <li>
          <a href="https://other.example.test/notice">2026年事业单位公开招聘公告</a>
          <span>2026-07-16</span>
        </li>
        <li><a href="/content/show?id=101">社会保险缴费基数调整通知</a><span>2026-07-15</span></li>
        </ul></body></html>
        """
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"content-type": "text/html; charset=utf-8"},
                    text=html,
                    request=request,
                )
            )
        )
        try:
            source = Source(
                **source_values()
                | {
                    "id": "changzhou-public-recruitment",
                    "name": "常州市事业单位公开招聘",
                    "kind": SourceKind.HTML_LIST,
                    "entry_url": "https://rsj.changzhou.gov.cn/recruitment",
                    "normalized_entry_url": "https://rsj.changzhou.gov.cn/recruitment",
                    "language": "zh-CN",
                    "config_json": (
                        '{"include_title_keywords":["事业单位","公务员","公开招聘"],'
                        '"timezone":"Asia/Shanghai"}'
                    ),
                }
            )
            collector = HTMLListCollector(SafeHttpFetcher(client, url_validator=AllowAllUrls()))
            result = await collector.collect(
                source,
                CollectionWindow(
                    start=datetime(2026, 7, 15, 0, 0, tzinfo=UTC),
                    end=datetime(2026, 7, 17, 0, 0, tzinfo=UTC),
                ),
            )
        finally:
            await client.aclose()

        assert result.error is None
        assert result.errors == ()
        assert len(result.candidates) == 1
        candidate = result.candidates[0]
        assert candidate.title == "2026年常州市事业单位公开招聘工作人员公告"
        assert candidate.url == "https://rsj.changzhou.gov.cn/content/show?id=100"
        assert candidate.published_at == datetime(2026, 7, 15, 16, tzinfo=UTC)
        assert candidate.language == "zh-CN"
        assert candidate.metadata == {"list_url": "https://rsj.changzhou.gov.cn/recruitment"}

    asyncio.run(scenario())


def test_content_extractor_returns_text_for_valid_html() -> None:
    """The extractor returns trafilatura-cleaned article text after a bounded HTTP fetch."""

    async def scenario() -> None:
        html = """
        <html><body><article><h1>DailyCast test</h1>
        <p>This is a substantial extraction paragraph about reliable news collection.</p>
        <p>It provides enough text for trafilatura to identify an article body.</p>
        </article></body></html>
        """
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"content-type": "text/html; charset=utf-8"},
                    text=html,
                    request=request,
                )
            )
        )
        try:
            result = await ContentExtractor(
                SafeHttpFetcher(client, url_validator=AllowAllUrls())
            ).extract("https://article.example.test/valid", FetchPolicy(timeout_seconds=2))
        finally:
            await client.aclose()

        assert result.error is None
        assert result.content_text is not None
        assert "reliable news collection" in result.content_text
        assert result.http_status == 200

    asyncio.run(scenario())


def test_content_extractor_classifies_timeout() -> None:
    """A transport timeout is returned as a retryable structured extraction error."""

    async def scenario() -> None:
        def timeout(_: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out")

        client = httpx.AsyncClient(transport=httpx.MockTransport(timeout))
        try:
            result = await ContentExtractor(
                SafeHttpFetcher(client, url_validator=AllowAllUrls())
            ).extract("https://article.example.test/timeout", FetchPolicy(timeout_seconds=1))
        finally:
            await client.aclose()

        assert result.error is not None
        assert result.error.code == "TIMEOUT"
        assert result.error.retryable is True

    asyncio.run(scenario())


def test_content_extractor_rejects_invalid_content_type() -> None:
    """Binary content never reaches trafilatura and is recorded as a content-level error."""

    async def scenario() -> None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"content-type": "application/pdf"},
                    content=b"not html",
                    request=request,
                )
            )
        )
        try:
            result = await ContentExtractor(
                SafeHttpFetcher(client, url_validator=AllowAllUrls())
            ).extract("https://article.example.test/binary", FetchPolicy(timeout_seconds=1))
        finally:
            await client.aclose()

        assert result.error is not None
        assert result.error.code == "UNSUPPORTED_CONTENT_TYPE"
        assert result.error.retryable is False

    asyncio.run(scenario())


def test_content_extractor_blocks_loopback_url_before_request() -> None:
    """SSRF policy rejects loopback targets before an HTTP request can be attempted."""

    async def scenario() -> None:
        result = await ContentExtractor(SafeHttpFetcher()).extract(
            "http://127.0.0.1/private", FetchPolicy(timeout_seconds=1)
        )

        assert result.error is not None
        assert result.error.code == "SSRF_BLOCKED"
        assert result.error.retryable is False

    asyncio.run(scenario())


def test_article_service_upserts_duplicate_normalized_url(app_config_path: Path) -> None:
    """Tracking parameters do not create duplicates and later discovery preserves content."""
    engine, factory = upgraded_factory(app_config_path)
    try:
        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            SourceRepository(unit.session).create(**source_values())

        service = ArticleService(factory)
        first = service.upsert_candidate(
            ArticleCandidate(
                source_id="hacker-news-rss",
                external_id="first-guid",
                url="https://article.example.test/path?b=2&utm_source=rss&a=1#fragment",
                title="A—Test Article!",
                summary="Initial summary",
                content_text="Persisted body text",
                published_at=datetime(2026, 7, 21, 10, 0, tzinfo=UTC),
            )
        )
        duplicate = service.upsert_candidate(
            ArticleCandidate(
                source_id="hacker-news-rss",
                external_id="second-guid",
                url="https://article.example.test/path?a=1&b=2",
                title="A Test Article",
                summary="Updated summary",
            )
        )

        assert duplicate.id == first.id
        assert first.normalized_title == normalize_title(first.title)
        assert first.title_hash == title_hash(normalize_title(first.title))
        assert duplicate.normalized_url == "https://article.example.test/path?a=1&b=2"
        assert duplicate.content_text == "Persisted body text"
        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            assert len(list(unit.session.scalars(select(Article)))) == 1
    finally:
        engine.dispose()


def test_rss_external_id_url_change_is_a_nonretryable_identity_conflict(
    app_config_path: Path,
) -> None:
    """A reused RSS GUID cannot silently overwrite the original Article URL identity."""
    engine, factory = upgraded_factory(app_config_path)
    try:
        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            SourceRepository(unit.session).create(**source_values())
        service = ArticleService(factory)
        first = service.upsert_candidate(
            ArticleCandidate(
                source_id="hacker-news-rss",
                external_id="stable-guid",
                url="https://article.example.test/original",
                title="Original article",
            )
        )

        with pytest.raises(ArticleValidationError) as raised:
            service.upsert_candidate(
                ArticleCandidate(
                    source_id="hacker-news-rss",
                    external_id="stable-guid",
                    url="https://article.example.test/moved",
                    title="Moved article",
                )
            )

        assert raised.value.error.code == "RSS_EXTERNAL_ID_URL_CONFLICT"
        assert raised.value.error.retryable is False
        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            persisted = ArticleRepository(unit.session).get(first.id)
            assert persisted is not None
            assert persisted.url == "https://article.example.test/original"
            assert len(ArticleRepository(unit.session).list()) == 1
    finally:
        engine.dispose()


def test_collection_pipeline_persists_articles_and_continues_after_one_extraction_failure(
    app_config_path: Path,
) -> None:
    """The real collecting/extracting flow records a failed Article without failing the task."""

    async def scenario() -> None:
        engine, factory = upgraded_factory(app_config_path)
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with UnitOfWork(factory) as unit:
                assert unit.session is not None
                SourceRepository(unit.session).create(**source_values())

            fetcher = SafeHttpFetcher(client, url_validator=AllowAllUrls())
            clock = FixedClock(datetime(2026, 7, 22, 12, tzinfo=UTC))
            article_service = ArticleService(factory)
            collection_service = SourceCollectionService(
                factory,
                {SourceKind.RSS: RSSCollector(fetcher)},
                article_service,
                clock=clock,
            )
            news_processor = NewsProcessor(
                factory,
                ProcessingPolicy(min_content_length=50, similarity_threshold=0.5),
                clock=clock,
            )
            orchestrator = PipelineOrchestrator(
                factory,
                build_collection_pipeline(
                    collection_service,
                    article_service,
                    ContentExtractor(fetcher),
                    news_processor,
                    AIEditorialService(factory, FakeRankingProvider()),
                    EpisodeService(factory),
                    FakeAudioGenerationService(),
                    FakePublicationDispatcher(),
                    BudgetController,
                    data_dir=app_config_path.parent / "work",
                    collection_window_hours=36,
                    clock=clock,
                ),
            )
            executor = InProcessTaskExecutor(factory, orchestrator)
            submission = TaskSubmissionService(factory, executor)
            await executor.start()
            task_run = submission.submit(
                TaskCommand(
                    task_type=TaskType.DAILY_GENERATE,
                    request={"edition": "daily", "episode_date": "2026-07-22"},
                    config_snapshot={"pipeline": "rss-v1"},
                    idempotency_key="collection-pipeline",
                    pipeline_version="rss-v1",
                )
            )
            for _ in range(100):
                with UnitOfWork(factory) as unit:
                    assert unit.session is not None
                    current = TaskRunRepository(unit.session).get(task_run.id)
                    assert current is not None
                    if current.status in {
                        TaskRunStatus.SUCCEEDED,
                        TaskRunStatus.SUCCEEDED_WITH_WARNINGS,
                    }:
                        break
                await asyncio.sleep(0.01)

            with UnitOfWork(factory) as unit:
                assert unit.session is not None
                current = TaskRunRepository(unit.session).get(task_run.id)
                assert current is not None
                assert (
                    current.status == TaskRunStatus.SUCCEEDED_WITH_WARNINGS
                ), current.error_summary
                assert [step.step_name for step in current.steps] == [
                    "collecting",
                    "extracting",
                    "filtering",
                    "deduplicating",
                    "clustering",
                    "ranking",
                    "outlining",
                    "scripting",
                    "checking",
                    "create_episode",
                    "generate_audio",
                    "publish",
                ]
                assert current.steps[1].warning_count == 1
                assert current.steps[2].warning_count == 1
                articles = list(unit.session.scalars(select(Article).order_by(Article.id)))
                assert [article.status for article in articles] == [
                    ArticleStatus.ELIGIBLE,
                    ArticleStatus.FILTERED,
                ]
                assert articles[0].news_event_id is not None
                assert articles[1].filter_reason == "MISSING_CONTENT"
                assert len(list(unit.session.scalars(select(NewsEvent)))) == 1
                outline_step = current.steps[6]
                scripting_step = current.steps[7]
                checking_step = current.steps[8]
                create_episode_step = current.steps[9]
                generate_audio_step = current.steps[10]
                publish_step = current.steps[11]
                assert outline_step.artifact_path == f"work/{task_run.id}/editorial/outline.json"
                outline_details = json.loads(outline_step.details_json)
                assert outline_details["source_article_count"] == 1
                assert outline_details["total_evidence_chars"] > 0
                script_path = f"work/{task_run.id}/editorial/script.json"
                assert scripting_step.artifact_path == script_path
                assert checking_step.artifact_path == f"work/{task_run.id}/editorial/review.json"
                artifact_root = app_config_path.parent / "work" / "work" / task_run.id / "editorial"
                script_payload = json.loads(
                    (artifact_root / "script.json").read_text(encoding="utf-8")
                )
                review_payload = json.loads(
                    (artifact_root / "review.json").read_text(encoding="utf-8")
                )
                metadata_payload = json.loads(
                    (artifact_root / "metadata.json").read_text(encoding="utf-8")
                )
                assert script_payload["schema_version"] == "1"
                assert review_payload["verdict"] == "pass"
                assert metadata_payload["schema_version"] == "1"
                checking_details = json.loads(checking_step.details_json)
                assert checking_details["review_verdict"] == "pass"
                assert checking_details["validation_issue_counts"]["blocking"] == 0
                create_episode_details = json.loads(create_episode_step.details_json)
                assert create_episode_step.output_count == 1
                assert create_episode_details["episode_id"] == current.episode_id
                assert isinstance(current.episode_id, int)
                episode = unit.session.get(Episode, current.episode_id)
                assert episode is not None
                assert episode.status is EpisodeStatus.REVIEW_REQUIRED
                assert len(episode.episode_items) == 1
                audio_details = json.loads(generate_audio_step.details_json)
                assert generate_audio_step.output_count == 1
                assert audio_details["episode_id"] == episode.id
                assert audio_details["segment_count"] == 3
                publish_details = json.loads(publish_step.details_json)
                assert publish_step.output_count == 0
                assert publish_details["skip_reason"] == "AUTO_PUBLISH_DISABLED"
            await executor.shutdown(grace_seconds=1)
        finally:
            await client.aclose()
            engine.dispose()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "feed.example.test":
            return httpx.Response(
                200,
                headers={"content-type": "application/rss+xml"},
                content=fixture_feed(),
                request=request,
            )
        if request.url.path == "/first":
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text=(
                    "<html><body><article><h1>Evidence</h1>"
                    "<p>First article has extractable collection evidence "
                    "with reliable context.</p>"
                    "<p>This second paragraph gives the extractor "
                    "sufficient editorial substance.</p>"
                    "</article></body></html>"
                ),
                request=request,
            )
        return httpx.Response(
            200,
            headers={"content-type": "application/pdf"},
            content=b"not an article",
            request=request,
        )

    asyncio.run(scenario())


class RecordingCollector:
    """Record which sources the collection service asks to collect."""

    def __init__(self) -> None:
        self.collected_source_ids: list[str] = []

    async def collect(self, source: Source, window: CollectionWindow) -> CollectionResult:
        """Return no candidates; only the collected source identity matters here."""
        del window
        self.collected_source_ids.append(source.id)
        return CollectionResult(source_id=source.id)


def test_collect_enabled_sources_excludes_briefing_tagged_sources(app_config_path: Path) -> None:
    """Briefing-only sources stay out of the podcast collection pool even when enabled."""
    engine, factory = upgraded_factory(app_config_path)
    try:
        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            SourceRepository(unit.session).create(**source_values())
            SourceRepository(unit.session).create(
                **{
                    **source_values(),
                    "id": "briefing-only-rss",
                    "entry_url": "https://briefing.example.test/rss",
                    "normalized_entry_url": "https://briefing.example.test/rss",
                    "config_json": json.dumps({"briefing_category": "ai"}),
                }
            )
        collector = RecordingCollector()
        collection_service = SourceCollectionService(
            factory, {SourceKind.RSS: collector}, ArticleService(factory)
        )
        window = CollectionWindow(
            start=datetime(2026, 8, 19, tzinfo=UTC),
            end=datetime(2026, 8, 20, tzinfo=UTC),
        )

        result = asyncio.run(collection_service.collect_enabled_sources(window))

        assert collector.collected_source_ids == ["hacker-news-rss"]
        assert result.source_count == 1
    finally:
        engine.dispose()
