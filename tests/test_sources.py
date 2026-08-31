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

from dailycast.core.config import WebResearchSettings, load_settings
from dailycast.core.errors import ConfigurationError
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
from dailycast.sources.bootstrap import (
    _load_source_configuration,
    _normalized_entry_url,
    load_configured_source_ids,
)
from dailycast.sources.contracts import ArticleCandidate, CollectionResult, CollectionWindow
from dailycast.sources.extraction import ContentExtractor, FetchPolicy, SafeHttpFetcher
from dailycast.sources.html_list import HTMLListCollector
from dailycast.sources.research import (
    ResearchCollector,
    ResearchSourceOptions,
    _candidate_url_error,
    _research_messages,
)
from dailycast.sources.rss import RSSCollector
from dailycast.sources.service import (
    ArticleService,
    ArticleValidationError,
    SourceCollectionService,
)
from dailycast.tts.service import AudioGenerationResult


def test_collection_window_excludes_the_next_day_midnight() -> None:
    """A daily briefing must not include an item at the following day's 00:00."""
    window = CollectionWindow(
        start=datetime(2026, 8, 24, 16, tzinfo=UTC),
        end=datetime(2026, 8, 25, 16, tzinfo=UTC),
    )

    assert window.includes(datetime(2026, 8, 25, 15, 59, 59, tzinfo=UTC))
    assert not window.includes(datetime(2026, 8, 25, 16, tzinfo=UTC))


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


def test_web_research_source_uses_a_non_fetchable_internal_identity() -> None:
    """Only the web-research collector may declare its stable research URI."""
    assert _normalized_entry_url("research://telecom", SourceKind.WEB_RESEARCH) == (
        "research://telecom"
    )
    with pytest.raises(ConfigurationError):
        _normalized_entry_url("research://telecom", SourceKind.RSS)


def test_briefing_source_configuration_keeps_web_research_out_of_podcast_seeds() -> None:
    """The management research sources are selected only by the briefing allowlist."""
    project_root = Path(__file__).resolve().parents[1]

    briefing_ids = load_configured_source_ids(project_root / "config" / "briefing.sources.yaml")
    podcast_ids = load_configured_source_ids(project_root / "config" / "sources.example.yaml")

    expected_research_ids = {
        "openai-web-research-telecom-management",
        "openai-web-research-ai-management",
    }
    expected_verified_source_ids = {"zte-official-news", "c114-operators", "leiphone-feed"}
    assert expected_research_ids.issubset(briefing_ids)
    assert expected_research_ids.isdisjoint(podcast_ids)
    assert expected_verified_source_ids.issubset(briefing_ids)
    assert expected_verified_source_ids.isdisjoint(podcast_ids)
    assert "openai-web-research-telecom" not in briefing_ids
    assert "openai-web-research-ai" not in briefing_ids
    assert "qbitai" not in briefing_ids
    assert "gsma-newsroom" not in briefing_ids
    assert "light-reading-telecom" not in briefing_ids


def test_checked_in_research_queries_keep_ai_global_but_sources_chinese() -> None:
    """Operator AI stays in telecom while global AI events use Chinese-language reporting."""
    project_root = Path(__file__).resolve().parents[1]
    sources = _load_source_configuration(project_root / "config" / "briefing.sources.yaml")
    source_by_id = {source.id: source for source in sources.sources}
    telecom_query = str(source_by_id["openai-web-research-telecom-management"].config["query"])
    ai_query = str(source_by_id["openai-web-research-ai-management"].config["query"])

    assert "Token" in telecom_query
    assert "运营商大模型" in telecom_query
    assert "大模型发布" in ai_query
    assert "热门应用" in ai_query
    assert "Claude" in ai_query
    assert "GPT" in ai_query
    assert "Gemini" in ai_query
    assert "中文" in ai_query
    assert "国内 AI 发展新闻" not in ai_query
    assert "中国移动" not in ai_query
    assert "中国电信" not in ai_query
    assert "中国联通" not in ai_query


def test_briefing_sources_do_not_apply_title_keyword_gates_before_editorial_review() -> None:
    """Only objective freshness/link checks may discard a briefing candidate locally."""
    project_root = Path(__file__).resolve().parents[1]
    sources = _load_source_configuration(project_root / "config" / "briefing.sources.yaml")

    assert all("include_title_keywords" not in source.config for source in sources.sources)


def test_briefing_sources_do_not_redeclare_a_default_seed_url_under_a_new_id() -> None:
    """A briefing source must not collide with an existing persistent source identity."""
    project_root = Path(__file__).resolve().parents[1]
    briefing_sources = _load_source_configuration(project_root / "config" / "briefing.sources.yaml")
    default_sources = _load_source_configuration(project_root / "config" / "sources.example.yaml")
    default_ids_by_url = {
        _normalized_entry_url(source.entry_url, source.kind): source.id
        for source in default_sources.sources
    }

    collisions = [
        (source.id, default_ids_by_url[normalized_url])
        for source in briefing_sources.sources
        if (normalized_url := _normalized_entry_url(source.entry_url, source.kind))
        in default_ids_by_url
        and default_ids_by_url[normalized_url] != source.id
    ]

    assert collisions == []


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


def test_rss_collector_marks_undated_entries_for_one_time_persistence() -> None:
    """An RSS entry without a date is retained but never gets a synthetic feed timestamp."""

    async def scenario() -> None:
        feed = (
            b'<?xml version="1.0" encoding="utf-8"?>'
            b'<rss version="2.0"><channel><title>undated</title>'
            b"<item><title>undated entry</title>"
            b"<link>https://news.example.test/undated</link>"
            b"<description>A summary long enough to become a candidate.</description></item>"
            b"</channel></rss>"
        )
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"content-type": "application/rss+xml"},
                    content=feed,
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
        assert len(result.candidates) == 1
        # Persistence assigns the first-discovery fallback exactly once.  The
        # collector must keep the absence of an upstream timestamp explicit so
        # later collection runs cannot make an old RSS entry look new again.
        assert result.candidates[0].published_at is None
        assert result.candidates[0].published_at_inferred is True

    asyncio.run(scenario())


def test_rss_collector_filters_configured_titles_before_persistence() -> None:
    """A broad hot-list feed retains only configured topical titles in its original order."""

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
            configured_source = Source(
                **source_values() | {"config_json": '{"include_title_keywords":["SECOND"]}'}
            )
            result = await RSSCollector(
                SafeHttpFetcher(client, url_validator=AllowAllUrls())
            ).collect(
                configured_source,
                CollectionWindow(
                    start=datetime(2026, 7, 21, 0, 0, tzinfo=UTC),
                    end=datetime(2026, 7, 22, 0, 0, tzinfo=UTC),
                ),
            )
        finally:
            await client.aclose()

        assert result.error is None
        assert [candidate.title for candidate in result.candidates] == [
            "Second collection candidate"
        ]

    asyncio.run(scenario())


def test_rsshub_route_requires_a_deployment_controlled_base_url() -> None:
    """An RSSHub route never falls back to an arbitrary public mirror."""

    async def scenario() -> None:
        route_source = Source(
            **source_values()
            | {
                "id": "kr36-hot-list",
                "entry_url": "rsshub://36kr/hot-list?limit=100",
                "normalized_entry_url": "rsshub://36kr/hot-list?limit=100",
            }
        )
        result = await RSSCollector(SafeHttpFetcher()).collect(
            route_source,
            CollectionWindow(
                start=datetime(2026, 7, 21, 0, 0, tzinfo=UTC),
                end=datetime(2026, 7, 22, 0, 0, tzinfo=UTC),
            ),
        )

        assert result.error is not None
        assert result.error.code == "RSSHUB_BASE_URL_REQUIRED"

    asyncio.run(scenario())


def test_rsshub_route_is_a_stable_source_identity() -> None:
    """The source seed accepts and canonicalizes RSSHub routes before database persistence."""
    assert _normalized_entry_url("rsshub://36KR/hot-list?b=2&a=1#fragment") == (
        "rsshub://36kr/hot-list?a=1&b=2"
    )


def test_rsshub_route_uses_configured_base_url_without_losing_its_query() -> None:
    """The logical route resolves through the selected instance before safe fetching."""

    async def scenario() -> None:
        requested_urls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested_urls.append(str(request.url))
            return httpx.Response(
                200,
                headers={"content-type": "application/rss+xml"},
                content=fixture_feed(),
                request=request,
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            route_source = Source(
                **source_values()
                | {
                    "id": "kr36-hot-list",
                    "entry_url": "rsshub://36kr/hot-list?limit=100",
                    "normalized_entry_url": "rsshub://36kr/hot-list?limit=100",
                }
            )
            result = await RSSCollector(
                SafeHttpFetcher(client, url_validator=AllowAllUrls()),
                rsshub_base_url="https://rsshub.example.test/private-instance",
            ).collect(
                route_source,
                CollectionWindow(
                    start=datetime(2026, 7, 21, 0, 0, tzinfo=UTC),
                    end=datetime(2026, 7, 22, 0, 0, tzinfo=UTC),
                ),
            )
        finally:
            await client.aclose()

        assert result.error is None
        assert requested_urls == [
            "https://rsshub.example.test/private-instance/36kr/hot-list?limit=100"
        ]

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


def test_html_list_collector_can_restrict_candidates_to_article_path_prefixes() -> None:
    """A newsroom list must not mistake its dated product-navigation links for news."""

    async def scenario() -> None:
        html = """
        <html><body><ul>
        <li>
          <a href="/content/zte-site/news/20260820.html">中兴通讯推进5G-A网络商用</a>
          <span>2026-08-20</span>
        </li>
        <li><a href="/china/solutions/5g.html">5G网络解决方案</a><span>2026-08-20</span></li>
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
                    "id": "zte-official-news",
                    "name": "中兴通讯新闻中心",
                    "kind": SourceKind.HTML_LIST,
                    "entry_url": "https://www.zte.com.cn/china/about/news.html",
                    "normalized_entry_url": "https://www.zte.com.cn/china/about/news.html",
                    "language": "zh-CN",
                    "config_json": (
                        '{"article_url_path_prefixes":["/content/zte-site/"],'
                        '"timezone":"Asia/Shanghai"}'
                    ),
                }
            )
            result = await HTMLListCollector(
                SafeHttpFetcher(client, url_validator=AllowAllUrls())
            ).collect(
                source,
                CollectionWindow(
                    start=datetime(2026, 8, 19, 0, 0, tzinfo=UTC),
                    end=datetime(2026, 8, 21, 0, 0, tzinfo=UTC),
                ),
            )
        finally:
            await client.aclose()

        assert result.error is None
        assert [candidate.url for candidate in result.candidates] == [
            "https://www.zte.com.cn/content/zte-site/news/20260820.html"
        ]

    asyncio.run(scenario())


def test_html_list_collector_rejects_an_undated_matching_navigation_link() -> None:
    """A list page's dated news cannot be diluted by an undated topic-navigation anchor."""

    async def scenario() -> None:
        html = """
        <html><body><nav><a href="/topics/5g">5G 专题</a></nav></body></html>
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
                    "id": "official-telecom-list",
                    "name": "官方通信列表",
                    "kind": SourceKind.HTML_LIST,
                    "entry_url": "https://official.example.test/news",
                    "normalized_entry_url": "https://official.example.test/news",
                    "language": "zh-CN",
                    "config_json": ('{"include_title_keywords":["5G"],"timezone":"Asia/Shanghai"}'),
                }
            )
            result = await HTMLListCollector(
                SafeHttpFetcher(client, url_validator=AllowAllUrls())
            ).collect(
                source,
                CollectionWindow(
                    start=datetime(2026, 7, 15, 0, 0, tzinfo=UTC),
                    end=datetime(2026, 7, 17, 0, 0, tzinfo=UTC),
                ),
            )
        finally:
            await client.aclose()

        assert result.error is None
        assert result.candidates == ()
        assert [error.code for error in result.errors] == ["MISSING_PUBLICATION_DATE"]

    asyncio.run(scenario())


def test_recruitment_source_uses_the_extended_tracking_window(app_config_path: Path) -> None:
    """Official recruitment tracking is not limited to the general breaking-news window."""

    class RecordingCollector:
        """Capture the effective collection window without making a network request."""

        def __init__(self) -> None:
            self.window: CollectionWindow | None = None

        async def collect(self, source: Source, window: CollectionWindow) -> CollectionResult:
            self.window = window
            return CollectionResult(source_id=source.id)

    async def scenario() -> None:
        engine, factory = upgraded_factory(app_config_path)
        try:
            with UnitOfWork(factory) as unit:
                assert unit.session is not None
                SourceRepository(unit.session).create(
                    **(
                        source_values()
                        | {
                            "id": "changzhou-public-recruitment",
                            "name": "常州市事业单位公开招聘",
                            "entry_url": "https://rsj.changzhou.gov.cn/recruitment",
                            "normalized_entry_url": "https://rsj.changzhou.gov.cn/recruitment",
                        }
                    )
                )
            collector = RecordingCollector()
            service = SourceCollectionService(
                factory,
                {SourceKind.RSS: collector},
                ArticleService(factory),
            )
            await service.collect_enabled_sources(
                CollectionWindow(
                    start=datetime(2026, 7, 21, 0, 0, tzinfo=UTC),
                    end=datetime(2026, 7, 22, 0, 0, tzinfo=UTC),
                )
            )

            assert collector.window == CollectionWindow(
                start=datetime(2026, 7, 8, 0, 0, tzinfo=UTC),
                end=datetime(2026, 7, 22, 0, 0, tzinfo=UTC),
            )
        finally:
            engine.dispose()

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
        assert result.published_at is None

    asyncio.run(scenario())


def test_content_extractor_honors_a_meta_declared_legacy_charset() -> None:
    """A legacy news page without an HTTP charset remains readable evidence."""

    async def scenario() -> None:
        html = """
        <html><head><meta http-equiv="content-type" content="text/html; charset=gb2312" /></head>
        <body><article><h1>通信网络建设进展</h1>
        <p>中国移动启动网络设备集采，项目覆盖多个省份的通信基础设施建设。</p>
        <p>此次采购明确了供货安排和后续交付节奏，相关单位将按计划推进。</p>
        </article></body></html>
        """.encode(
            "gb2312"
        )
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"content-type": "text/html"},
                    content=html,
                    request=request,
                )
            )
        )
        try:
            result = await ContentExtractor(
                SafeHttpFetcher(client, url_validator=AllowAllUrls())
            ).extract("https://article.example.test/legacy-charset", FetchPolicy(timeout_seconds=2))
        finally:
            await client.aclose()

        assert result.error is None
        assert result.content_text is not None
        assert "中国移动启动网络设备集采" in result.content_text
        assert "�" not in result.content_text

    asyncio.run(scenario())


def test_content_extractor_reads_a_verified_publication_time_from_article_metadata() -> None:
    """Web-research sources may use only a publication time visible in the fetched page."""

    async def scenario() -> None:
        html = """
        <html><head>
        <meta property="article:published_time" content="2026-08-20T08:15:00+08:00">
        </head><body><article><h1>DailyCast test</h1>
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
            ).extract("https://article.example.test/published", FetchPolicy(timeout_seconds=2))
        finally:
            await client.aclose()

        assert result.error is None
        assert result.published_at == datetime(2026, 8, 20, 0, 15, tzinfo=UTC)

    asyncio.run(scenario())


def test_content_extractor_reads_a_labelled_visible_publication_time() -> None:
    """A page may expose its publication time in a labelled visible element."""

    async def scenario() -> None:
        html = """
        <html><body><article><h1>DailyCast test</h1>
        <div class="article-public-date-label">发布日期:</div><span>2026-08-19 07:30</span>
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
            ).extract(
                "https://article.example.test/visible-published", FetchPolicy(timeout_seconds=2)
            )
        finally:
            await client.aclose()

        assert result.error is None
        assert result.published_at == datetime(2026, 8, 19, 7, 30, tzinfo=UTC)

    asyncio.run(scenario())


def test_content_extractor_reads_an_explicit_publishdate_meta_tag() -> None:
    """Chinese news sites often expose the verified date as meta[name=publishdate]."""

    async def scenario() -> None:
        html = """
        <html><head><meta name="publishdate" content="2026-08-25"></head>
        <body><article><h1>中国移动人工智能应用动态</h1>
        <p>中国移动在国内推出人工智能应用，并公布后续服务计划。</p>
        <p>该项目已经完成上线验证，面向实际用户提供服务。</p>
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
            ).extract(
                "https://jx.cnr.cn/meta-publishdate",
                FetchPolicy(timeout_seconds=2),
            )
        finally:
            await client.aclose()

        assert result.error is None
        assert result.published_at == datetime(2026, 8, 24, 16, tzinfo=UTC)

    asyncio.run(scenario())


def test_content_extractor_reads_a_date_immediately_followed_by_a_source_label() -> None:
    """A dated article header followed by 来源 is an explicit publication signal."""

    async def scenario() -> None:
        html = """
        <html><body><article><h1>移动技术赋能产业现场</h1>
        <div class="article-header">2026-08-25 09:26 来源：北国网</div>
        <p>中国移动将人工智能能力用于国内产业现场，并完成项目部署。</p>
        <p>项目披露了服务范围和当前运行情况。</p>
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
            ).extract(
                "https://economy.lnd.com.cn/source-labelled-date",
                FetchPolicy(timeout_seconds=2),
            )
        finally:
            await client.aclose()

        assert result.error is None
        assert result.published_at == datetime(2026, 8, 25, 1, 26, tzinfo=UTC)

    asyncio.run(scenario())


def test_content_extractor_reads_the_c114_article_header_time() -> None:
    """C114's unlabelled article-header time is verified only for its own article pages."""

    async def scenario() -> None:
        html = """
        <html><body><article><div class="article_top">
        <div class="time">2026/8/20 14:43</div>
        <h1 class="article_title">中国移动网络建设动态</h1>
        </div>
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
            ).extract(
                "https://www.c114.com.cn/news/118/a1316017.html",
                FetchPolicy(timeout_seconds=2),
            )
        finally:
            await client.aclose()

        assert result.error is None
        assert result.published_at == datetime(2026, 8, 20, 6, 43, tzinfo=UTC)

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


def test_content_extractor_rejects_a_verification_challenge_page() -> None:
    """A 200 verification page must not masquerade as an accessible news article."""

    async def scenario() -> None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"content-type": "text/html; charset=utf-8"},
                    text=(
                        "<html><head><title>Just a moment...</title></head>"
                        "<body><h1>Security check</h1><p>请完成验证码验证后继续访问。</p>"
                        "</body></html>"
                    ),
                    request=request,
                )
            )
        )
        try:
            result = await ContentExtractor(
                SafeHttpFetcher(client, url_validator=AllowAllUrls())
            ).extract("https://article.example.test/challenge", FetchPolicy(timeout_seconds=1))
        finally:
            await client.aclose()

        assert result.error is not None
        assert result.error.code == "ACCESS_CHALLENGE"
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


def test_telecom_research_prompt_requires_multifacet_html_articles() -> None:
    """Native search must cover the management facets instead of returning one PDF hit."""
    messages = _research_messages(
        ResearchSourceOptions(
            briefing_category="telecom",
            topic="telecom",
            query="通信行业过去24小时重要动态",
            publisher_preference="regulator_operator_vendor_first_party",
            require_verified_publication_date=True,
        ),
        CollectionWindow(
            start=datetime(2026, 8, 24, 16, tzinfo=UTC),
            end=datetime(2026, 8, 25, 16, tzinfo=UTC),
        ),
    )

    assert "分别检索" in messages[-1].content
    assert "运营商" in messages[-1].content
    assert "基站" in messages[-1].content
    assert "竞争对手" in messages[-1].content
    assert "政策" in messages[-1].content
    assert "常州" in messages[-1].content
    assert "江苏" in messages[-1].content
    assert "其他地级市" in messages[-1].content
    assert "全国" in messages[-1].content
    assert "中国移动" in messages[-1].content
    assert "中国电信" in messages[-1].content
    assert "中国联通" in messages[-1].content
    assert "北京时间：2026-08-25 00:00 至 2026-08-26 00:00" in messages[-1].content
    assert "2026-08-24T16:00:00+00:00" not in messages[-1].content
    assert "国内外运营商" not in messages[-1].content
    assert "HTML" in messages[-1].content
    assert "PDF" in messages[-1].content


def test_ai_research_prompt_covers_global_models_through_chinese_sources() -> None:
    """AI discovery covers Chinese and global events while restricting source language."""
    messages = _research_messages(
        ResearchSourceOptions(
            briefing_category="ai",
            topic="ai",
            query="中文来源报道的全球 AI 动态",
            publisher_preference="company_regulator_research_first_party",
            require_verified_publication_date=True,
        ),
        CollectionWindow(
            start=datetime(2026, 8, 20, 9, tzinfo=UTC),
            end=datetime(2026, 8, 21, 9, tzinfo=UTC),
        ),
    )

    prompt = messages[-1].content
    assert "字节" in prompt
    assert "腾讯" in prompt
    assert "华为" in prompt
    assert "小米" in prompt
    assert "OpenAI" in prompt
    assert "Anthropic" in prompt
    assert "Google" in prompt
    assert "GPT" in prompt
    assert "Claude" in prompt
    assert "Gemini" in prompt
    assert "大模型" in prompt
    assert "开源" in prompt
    assert "本地化" in prompt
    assert "热门应用" in prompt
    assert "智能体" in prompt
    assert "中文页面" in prompt
    assert "中国移动" not in prompt
    assert "中国电信" not in prompt
    assert "中国联通" not in prompt


def test_ai_research_collector_rejects_a_foreign_language_source_page() -> None:
    """A global AI event is eligible only when its reader-facing source page is Chinese."""

    class TwoLanguageWebResearchProvider:
        provider_name = "openai_responses"
        model = "test-model"

        async def generate_web_research(
            self,
            messages: tuple[LLMMessage, ...],
            response_schema: type[BaseModel],
            model_options: dict[str, object],
        ) -> StructuredResult:
            del messages, response_schema, model_options
            return StructuredResult(
                content={
                    "candidates": [
                        {
                            "title": "Claude 发布新一代企业模型",
                            "url": "https://publisher.example.test/english",
                            "publisher": "Foreign Tech",
                            "finding": "Claude 发布新模型。",
                            "published_at_hint": "2026-08-20T08:15:00+08:00",
                        },
                        {
                            "title": "GPT 推出新的企业智能体能力",
                            "url": "https://publisher.example.test/chinese",
                            "publisher": "中文科技媒体",
                            "finding": "GPT 企业智能体能力正式发布。",
                            "published_at_hint": "2026-08-20T08:15:00+08:00",
                        },
                    ]
                },
                model=self.model,
                usage=LLMUsage(input_tokens=1, output_tokens=1),
                request_id="two-language-research",
            )

    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/english":
                body = (
                    "Anthropic released a new Claude model for enterprise workflows. "
                    "The article explains availability, pricing, deployment and customer use cases."
                )
            else:
                body = (
                    "中文科技媒体报道，GPT 推出新的企业智能体能力，并公布产品开放范围。"
                    "该能力面向真实业务流程，文章说明了上线时间、使用方式和企业部署安排。"
                )
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                text=(
                    '<html><head><meta property="article:published_time" '
                    'content="2026-08-20T08:15:00+08:00"></head>'
                    f"<body><article><p>{body}</p><p>{body}</p></article></body></html>"
                ),
                request=request,
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            source = Source(
                **{
                    **source_values(),
                    "id": "openai-web-research-ai-chinese-sources",
                    "kind": SourceKind.WEB_RESEARCH,
                    "entry_url": "research://ai-chinese-sources",
                    "normalized_entry_url": "research://ai-chinese-sources",
                    "language": "zh-CN",
                    "config_json": json.dumps(
                        {
                            "briefing_category": "ai",
                            "topic": "ai",
                            "query": "中文来源报道的全球 AI 动态",
                            "publisher_preference": "chinese_language_sources",
                            "require_verified_publication_date": True,
                        }
                    ),
                    "max_items_per_run": 2,
                }
            )
            result = await ResearchCollector(
                TwoLanguageWebResearchProvider(),
                ContentExtractor(SafeHttpFetcher(client, url_validator=AllowAllUrls())),
                WebResearchSettings(enabled=True, max_search_calls_per_source=1),
            ).collect(
                source,
                CollectionWindow(
                    start=datetime(2026, 8, 19, 9, tzinfo=UTC),
                    end=datetime(2026, 8, 20, 9, tzinfo=UTC),
                ),
            )
        finally:
            await client.aclose()

        assert [candidate.title for candidate in result.candidates] == [
            "GPT 推出新的企业智能体能力"
        ]
        assert "NON_CHINESE_SOURCE" in [error.code for error in result.errors]

    asyncio.run(scenario())


def test_research_rejects_a_reader_domain_known_to_be_unreachable() -> None:
    """A server-reachable page is not enough when the audience cannot open its source link."""
    error = _candidate_url_error("https://www.qbitai.com/2026/08/478191.html")

    assert error is not None
    assert error.code == "READER_URL_BLOCKED"


def test_research_collector_runs_bounded_search_calls_across_telecom_facets() -> None:
    """A one-result native search cannot starve every management-relevant telecom direction."""

    class FacetedWebResearchProvider:
        provider_name = "openai_responses"
        model = "test-model"

        def __init__(self) -> None:
            self.messages: list[tuple[LLMMessage, ...]] = []
            self.model_options: list[dict[str, object]] = []

        async def generate_web_research(
            self,
            messages: tuple[LLMMessage, ...],
            response_schema: type[BaseModel],
            model_options: dict[str, object],
        ) -> StructuredResult:
            del response_schema
            self.messages.append(messages)
            self.model_options.append(model_options)
            index = len(self.messages)
            return StructuredResult(
                content={
                    "candidates": [
                        {
                            "title": f"通信管理候选 {index}-{candidate_index}",
                            "url": f"https://publisher.example.test/article-{index}-{candidate_index}",
                            "publisher": "运营商公告",
                            "finding": "运营商公告披露了网络建设和交付安排。",
                            "published_at_hint": "2026-08-20T08:15:00+08:00",
                        }
                        for candidate_index in (1, 2)
                    ]
                },
                model=self.model,
                usage=LLMUsage(input_tokens=1, output_tokens=1),
                request_id=f"research-request-{index}",
            )

    async def scenario() -> None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"content-type": "text/html; charset=utf-8"},
                    text="""
                    <html><head><meta property="article:published_time"
                    content="2026-08-20T08:15:00+08:00"></head><body><article>
                    <p>运营商公告披露了网络建设和交付安排，正文提供足够事实供日报使用。</p>
                    <p>第二段确保这是一篇可验证的新闻正文而不是搜索结果页。</p>
                    </article></body></html>
                    """,
                    request=request,
                )
            )
        )
        provider = FacetedWebResearchProvider()
        source = Source(
            **{
                **source_values(),
                "id": "openai-web-research-telecom-facets",
                "kind": SourceKind.WEB_RESEARCH,
                "entry_url": "research://telecom-facets",
                "normalized_entry_url": "research://telecom-facets",
                "config_json": json.dumps(
                    {
                        "briefing_category": "telecom",
                        "topic": "telecom",
                        "query": "通信行业过去24小时重要动态",
                        "publisher_preference": "regulator_operator_vendor_first_party",
                        "require_verified_publication_date": True,
                    }
                ),
                "max_items_per_run": 4,
            }
        )
        try:
            result = await ResearchCollector(
                provider,
                ContentExtractor(SafeHttpFetcher(client, url_validator=AllowAllUrls())),
                WebResearchSettings(enabled=True, max_search_calls_per_source=4),
            ).collect(
                source,
                CollectionWindow(
                    start=datetime(2026, 8, 19, 9, tzinfo=UTC),
                    end=datetime(2026, 8, 20, 9, tzinfo=UTC),
                ),
            )
        finally:
            await client.aclose()

        assert len(provider.messages) == 4
        assert len(result.candidates) == 4
        assert [candidate.url for candidate in result.candidates] == [
            "https://publisher.example.test/article-1-1",
            "https://publisher.example.test/article-2-1",
            "https://publisher.example.test/article-3-1",
            "https://publisher.example.test/article-4-1",
        ]
        assert all("本轮重点" in messages[-1].content for messages in provider.messages)
        assert all(
            options == {"search_context_size": "medium"} for options in provider.model_options
        )

    asyncio.run(scenario())


def test_research_collector_persists_only_a_locally_verified_final_article() -> None:
    """A model candidate becomes a source candidate only after final-page verification."""

    class FakeWebResearchProvider:
        provider_name = "openai_responses"
        model = "test-model"

        async def generate_web_research(
            self,
            messages: tuple[LLMMessage, ...],
            response_schema: type[BaseModel],
            model_options: dict[str, object],
        ) -> StructuredResult:
            assert "通信行业" in messages[-1].content
            assert "12 至 20 条" in messages[-1].content
            assert "逐项覆盖" in messages[-1].content
            assert model_options["search_context_size"] == "medium"
            assert response_schema.__name__ == "WebResearchCandidateSet"
            return StructuredResult(
                content={
                    "candidates": [
                        {
                            "title": "运营商发布网络升级计划",
                            "url": "https://publisher.example.test/redirect",
                            "publisher": "运营商公告",
                            "finding": "网络升级计划已在当日公告中披露。",
                            "published_at_hint": "2026-08-20T08:15:00+08:00",
                        }
                    ]
                },
                model=self.model,
                usage=LLMUsage(input_tokens=1, output_tokens=1),
                request_id="research-request",
            )

    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/redirect":
                return httpx.Response(
                    302,
                    headers={"location": "/final"},
                    request=request,
                )
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                text="""
                <html><head>
                <meta property="article:published_time" content="2026-08-20T08:15:00+08:00">
                </head><body><article><h1>运营商发布网络升级计划</h1>
                <p>运营商公告披露了网络升级计划和明确的落地安排。</p>
                <p>第二段提供足够文本，确保正文提取可验证且不是跳转页面。</p>
                </article></body></html>
                """,
                request=request,
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            source = Source(
                **{
                    **source_values(),
                    "id": "openai-web-research-telecom",
                    "name": "网页研究·通信行业",
                    "kind": SourceKind.WEB_RESEARCH,
                    "entry_url": "research://telecom",
                    "normalized_entry_url": "research://telecom",
                    "config_json": json.dumps(
                        {
                            "briefing_category": "telecom",
                            "topic": "telecom",
                            "query": "通信行业过去24小时重要动态",
                            "publisher_preference": "regulator_operator_vendor_first_party",
                            "require_verified_publication_date": True,
                        }
                    ),
                }
            )
            collector = ResearchCollector(
                FakeWebResearchProvider(),
                ContentExtractor(SafeHttpFetcher(client, url_validator=AllowAllUrls())),
                WebResearchSettings(enabled=True),
            )
            result = await collector.collect(
                source,
                CollectionWindow(
                    start=datetime(2026, 8, 19, 9, tzinfo=UTC),
                    end=datetime(2026, 8, 20, 9, tzinfo=UTC),
                ),
            )
        finally:
            await client.aclose()

        assert result.error is None
        assert result.errors == ()
        assert len(result.candidates) == 1
        candidate = result.candidates[0]
        assert candidate.url == "https://publisher.example.test/final"
        assert candidate.published_at == datetime(2026, 8, 20, 0, 15, tzinfo=UTC)
        assert candidate.http_status == 200
        assert candidate.fetched_at is not None
        assert candidate.metadata["request_id"] == "research-request"
        assert candidate.metadata["candidate_url"] == "https://publisher.example.test/redirect"

    asyncio.run(scenario())


def test_research_collector_accepts_an_empty_discovery_result() -> None:
    """A quiet day is a successful empty research result, not a malformed provider response."""

    class EmptyWebResearchProvider:
        provider_name = "openai_responses"
        model = "test-model"

        async def generate_web_research(
            self,
            messages: tuple[LLMMessage, ...],
            response_schema: type[BaseModel],
            model_options: dict[str, object],
        ) -> StructuredResult:
            del messages, response_schema, model_options
            return StructuredResult(
                content={"candidates": []},
                model=self.model,
                usage=LLMUsage(),
                request_id="quiet-day",
            )

    source = Source(
        **{
            **source_values(),
            "id": "openai-web-research-ai",
            "kind": SourceKind.WEB_RESEARCH,
            "entry_url": "research://ai",
            "normalized_entry_url": "research://ai",
            "config_json": json.dumps(
                {
                    "briefing_category": "ai",
                    "topic": "ai",
                    "query": "AI 行业过去24小时重要动态",
                    "publisher_preference": "company_regulator_research_first_party",
                    "require_verified_publication_date": True,
                }
            ),
        }
    )
    collector = ResearchCollector(
        EmptyWebResearchProvider(),
        ContentExtractor(SafeHttpFetcher()),
        WebResearchSettings(enabled=True),
    )

    result = asyncio.run(
        collector.collect(
            source,
            CollectionWindow(
                start=datetime(2026, 8, 19, 9, tzinfo=UTC),
                end=datetime(2026, 8, 20, 9, tzinfo=UTC),
            ),
        )
    )

    assert result.error is None
    assert result.candidates == ()
    assert result.errors == ()


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


def test_article_service_preserves_the_first_inferred_timestamp(app_config_path: Path) -> None:
    """Repeated undated RSS observations cannot refresh an article into the current window."""
    engine, factory = upgraded_factory(app_config_path)
    try:
        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            SourceRepository(unit.session).create(**source_values())

        first_observed_at = datetime(2026, 7, 21, 10, 0, tzinfo=UTC)
        later_observed_at = datetime(2026, 7, 22, 10, 0, tzinfo=UTC)
        first = ArticleService(factory, clock=FixedClock(first_observed_at)).upsert_candidate(
            ArticleCandidate(
                source_id="hacker-news-rss",
                url="https://article.example.test/undated",
                title="Undated feed entry",
                published_at_inferred=True,
            )
        )
        refreshed = ArticleService(factory, clock=FixedClock(later_observed_at)).upsert_candidate(
            ArticleCandidate(
                source_id="hacker-news-rss",
                url="https://article.example.test/undated",
                title="Undated feed entry",
                published_at_inferred=True,
            )
        )

        assert first.published_at == first_observed_at
        assert first.published_at_inferred is True
        assert refreshed.published_at == first_observed_at.replace(tzinfo=None)
        assert refreshed.published_at_inferred is True
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
