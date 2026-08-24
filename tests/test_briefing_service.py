"""Briefing service end-to-end tests with a real database and fake network edges."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from editorial_test_support import upgraded_session_factory
from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker

from dailycast.briefing.renderer import RENDER_BYTE_BUDGET
from dailycast.briefing.selection import BriefingSelectionPolicy, load_selection_policy
from dailycast.briefing.service import (
    ALREADY_COMPLETED,
    BriefingRunInProgressError,
    BriefingService,
    read_briefings_for_date,
)
from dailycast.briefing.webhook import WebhookNotifier
from dailycast.core.errors import LLMProviderError
from dailycast.core.time import Clock
from dailycast.db.models import LLMOperation, Source, SourceKind
from dailycast.db.repositories import SourceRepository
from dailycast.db.transactions import UnitOfWork
from dailycast.llm.budget import BudgetController, estimate_message_input_tokens
from dailycast.llm.contracts import LLMMessage, LLMProvider, LLMUsage, StructuredResult
from dailycast.llm.providers.failover import FailoverLLMProvider
from dailycast.news.service import NewsProcessor
from dailycast.news.types import ProcessingPolicy
from dailycast.sources.contracts import (
    ArticleCandidate,
    CollectionResult,
    CollectionWindow,
    ExtractedArticle,
    SourceError,
)
from dailycast.sources.extraction import ContentExtractor, FetchPolicy
from dailycast.sources.service import ArticleService, SourceCollectionService

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeRSSCollector:
    """Return canned candidates per source without any network access."""

    def __init__(self, candidates_by_source: Mapping[str, Sequence[ArticleCandidate]]) -> None:
        self._candidates_by_source = candidates_by_source
        self.collected_source_ids: list[str] = []
        self.collection_windows: list[CollectionWindow] = []

    async def collect(self, source: Source, window: CollectionWindow) -> CollectionResult:
        """Record which sources the briefing flow actually asked to collect."""
        self.collected_source_ids.append(source.id)
        self.collection_windows.append(window)
        return CollectionResult(
            source_id=source.id,
            candidates=tuple(self._candidates_by_source.get(source.id, ())),
        )


class FixedClock(Clock):
    """Return one deterministic instant for briefing freshness tests."""

    def __init__(self, value: datetime) -> None:
        self._value = value

    def now(self) -> datetime:
        return self._value


class FakeExtractor(ContentExtractor):
    """Return a canned body for any article that still lacks one."""

    def __init__(self, content: str, *, published_at: datetime | None = None) -> None:
        self._content = content
        self._published_at = published_at

    async def extract(self, url: str, policy: FetchPolicy) -> ExtractedArticle:
        """Pretend every fetch succeeded so tests never touch the network."""
        del policy
        return ExtractedArticle(
            requested_url=url,
            final_url=url,
            content_text=self._content,
            http_status=200,
            fetched_at=datetime.now(UTC),
            published_at=self._published_at,
        )


class UnreachableLinkExtractor:
    """Reject an article page that cannot be opened by the delivery environment."""

    async def extract(self, url: str, policy: FetchPolicy) -> ExtractedArticle:
        del policy
        return ExtractedArticle(
            requested_url=url,
            final_url=None,
            content_text=None,
            http_status=None,
            fetched_at=None,
            error=SourceError(
                code="NETWORK_ERROR",
                summary="source request failed: ReadError",
                retryable=True,
            ),
        )


class FakeBriefingLLM:
    """Match one canned structured result per category title found in the prompt."""

    provider_name = "fake"
    model = "fake-briefing-model"
    max_output_tokens = 100

    def __init__(self, results_by_marker: Mapping[str, object]) -> None:
        self._results_by_marker = results_by_marker
        self.operations: list[LLMOperation] = []
        self.user_prompts: list[str] = []

    def generation_config_hash(self, model_options: Mapping[str, object]) -> str:
        """Return a stable identity; briefing never uses the artifact cache."""
        del model_options
        return "fake-briefing-config"

    async def generate_structured(
        self,
        operation: LLMOperation,
        messages: Sequence[LLMMessage],
        response_schema: type[BaseModel],
        model_options: Mapping[str, object],
    ) -> StructuredResult:
        """Return the canned payload keyed by the category title in the user prompt."""
        del response_schema, model_options
        self.operations.append(operation)
        user_content = messages[-1].content
        self.user_prompts.append(user_content)
        for marker, value in self._results_by_marker.items():
            if marker in user_content:
                if isinstance(value, Exception):
                    raise value
                return StructuredResult(
                    content=value,  # type: ignore[arg-type]
                    model=self.model,
                    usage=LLMUsage(input_tokens=10, output_tokens=20),
                    request_id="fake-briefing-1",
                )
        raise AssertionError("no fake LLM result matched the briefing prompt")


class RecordingNotifier(WebhookNotifier):
    """Capture pushed markdown without a webhook."""

    def __init__(self) -> None:
        self.pushed: list[str] = []

    async def push(self, markdown: str) -> None:
        """Record the exact markdown that would have been sent to the webhook."""
        self.pushed.append(markdown)


class FailingNotifier(WebhookNotifier):
    """Fail every push so completion markers must stay absent."""

    def __init__(self) -> None:
        self.attempts = 0

    async def push(self, markdown: str) -> None:
        """Count each attempt before reporting a webhook outage."""
        del markdown
        self.attempts += 1
        raise RuntimeError("webhook down")


def _seed_source(
    factory: sessionmaker[Session],
    source_id: str,
    *,
    category: str | None,
    priority: int = 80,
) -> None:
    config = {"briefing_category": category} if category is not None else {}
    with UnitOfWork(factory) as unit:
        assert unit.session is not None
        SourceRepository(unit.session).create(
            id=source_id,
            name=f"来源 {source_id}",
            kind=SourceKind.RSS,
            entry_url=f"https://{source_id}.example.test/rss",
            normalized_entry_url=f"https://{source_id}.example.test/rss",
            priority=priority,
            config_json=json.dumps(config),
        )


def _candidate(
    source_id: str,
    key: str,
    *,
    title: str | None = None,
    content_text: str | None = None,
    published_at: datetime | None = None,
) -> ArticleCandidate:
    is_ai = "ai" in source_id
    return ArticleCandidate(
        source_id=source_id,
        url=f"https://{source_id}.example.test/{key}",
        title=title or (f"DeepSeek 发布大模型 {key}" if is_ai else f"中国移动 {key} 网络建设进展"),
        content_text=content_text or (f"evidence-{key} " * 100),
        published_at=published_at or datetime.now(UTC),
    )


def _llm_payload(source_url: str, source_name: str) -> dict[str, object]:
    return {
        "overview": "今天该类目整体平稳，值得关注以下几件事。",
        "items": [
            {
                "headline": "一句话头条",
                "summary": "第一句摘要。第二句摘要。",
                "why_it_matters": "这件事会影响团队接下来一周的产品与采购判断。",
                "source_name": source_name,
                "source_url": source_url,
            }
        ],
    }


def _llm_payloads(source_urls: Sequence[str], source_name: str) -> dict[str, object]:
    """Return one detailed evidence-backed entry for every supplied article URL."""
    return {
        "overview": "今天该类目的重要进展集中在网络、算力和智能服务的协同演进。",
        "items": [
            {
                "headline": f"第 {index} 条重要进展",
                "summary": "相关主体公布最新进展并披露了覆盖范围、业务数据和下一步安排，"
                "这些信息反映当前项目已从规划进入实际推进阶段。",
                "why_it_matters": "它会影响行业下一阶段的产品投入与采购判断。",
                "source_name": source_name,
                "source_url": source_url,
            }
            for index, source_url in enumerate(source_urls, start=1)
        ],
    }


def _build_service(
    factory: sessionmaker[Session],
    output_dir: Path,
    *,
    collector: FakeRSSCollector,
    llm: LLMProvider,
    notifier: WebhookNotifier | None,
    extractor: ContentExtractor | None = None,
    budget_factory: Callable[[], BudgetController] = BudgetController,
    briefing_source_ids: frozenset[str] | None = None,
    selection_policy: BriefingSelectionPolicy | None = None,
    clock: Clock | None = None,
) -> BriefingService:
    article_service = ArticleService(factory, clock=clock)
    collection_service = SourceCollectionService(
        factory, {SourceKind.RSS: collector}, article_service, clock=clock
    )
    news_processor = NewsProcessor(
        factory,
        ProcessingPolicy(max_age_hours=72, min_content_length=10, similarity_threshold=0.58),
        clock=clock,
    )
    return BriefingService(
        factory,
        collection_service,
        article_service,
        extractor or FakeExtractor("抓取的正文内容。" * 10),
        news_processor,
        llm,
        notifier,
        output_dir=output_dir,
        budget_factory=budget_factory,
        briefing_source_ids=briefing_source_ids,
        selection_policy=selection_policy
        or load_selection_policy(PROJECT_ROOT / "config" / "briefing.selection.yaml"),
        clock=clock,
    )


@pytest.fixture
def session_factory(app_config_path: Path) -> sessionmaker[Session]:
    """Create one upgraded SQLite database per test."""
    return upgraded_session_factory(app_config_path)


def test_briefing_run_generates_pushes_and_persists_every_category(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """A full run collects only tagged sources and ships one file per category."""
    _seed_source(session_factory, "telecom-source", category="telecom")
    _seed_source(session_factory, "ai-source", category="ai")
    _seed_source(session_factory, "podcast-source", category=None)
    collector = FakeRSSCollector(
        {
            "telecom-source": [_candidate("telecom-source", f"t{index}") for index in range(1, 6)],
            "ai-source": [_candidate("ai-source", f"a{index}") for index in range(1, 6)],
            "podcast-source": [_candidate("podcast-source", "p1")],
        }
    )
    llm = FakeBriefingLLM(
        {
            "通信行业日报": _llm_payloads(
                [f"https://telecom-source.example.test/t{index}" for index in range(1, 6)],
                "来源 telecom-source",
            ),
            "AI 动态日报": _llm_payloads(
                [f"https://ai-source.example.test/a{index}" for index in range(1, 6)],
                "来源 ai-source",
            ),
        }
    )
    notifier = RecordingNotifier()
    output_dir = tmp_path / "briefings"
    service = _build_service(
        session_factory, output_dir, collector=collector, llm=llm, notifier=notifier
    )

    report = asyncio.run(service.run())

    assert sorted(collector.collected_source_ids) == ["ai-source", "telecom-source"]
    assert llm.operations == [LLMOperation.GENERATE_BRIEFING, LLMOperation.GENERATE_BRIEFING]
    statuses = {entry.category: entry.status for entry in report.categories}
    assert statuses == {"telecom": "generated", "ai": "generated"}
    for entry in report.categories:
        assert entry.push_status == "sent"
        assert entry.file_path is not None and entry.file_path.is_file()
    assert len(notifier.pushed) == 2
    briefings = read_briefings_for_date(output_dir, date.fromisoformat(report.date))
    assert set(briefings) == {"telecom", "ai"}
    assert "# 通信行业日报" in briefings["telecom"]
    assert (
        sum(
            f"https://telecom-source.example.test/t{index}" in briefings["telecom"]
            for index in range(1, 6)
        )
        == 2
    )
    assert (
        sum(f"https://ai-source.example.test/a{index}" in briefings["ai"] for index in range(1, 6))
        == 1
    )
    assert notifier.pushed[0].startswith("# 通信行业日报")
    assert notifier.pushed[1].startswith("# AI 动态日报")
    assert all(len(markdown.encode("utf-8")) <= RENDER_BYTE_BUDGET for markdown in notifier.pushed)


def test_briefing_run_trims_overlong_model_prose_and_still_pushes(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """A verbose model response is compacted for delivery instead of losing its category."""
    _seed_source(session_factory, "ai-source", category="ai")
    source_url = "https://ai-source.example.test/a1"
    payload = _llm_payload(source_url, "来源 ai-source")
    item = payload["items"][0]  # type: ignore[index]
    assert isinstance(item, dict)
    item["summary"] = "本地部署进展" * 100
    collector = FakeRSSCollector({"ai-source": [_candidate("ai-source", "a1")]})
    llm = FakeBriefingLLM({"AI 动态日报": payload})
    notifier = RecordingNotifier()
    service = _build_service(
        session_factory,
        tmp_path / "briefings",
        collector=collector,
        llm=llm,
        notifier=notifier,
    )

    report = asyncio.run(service.run())

    ai_report = next(entry for entry in report.categories if entry.category == "ai")
    assert ai_report.status == "generated"
    assert ai_report.push_status == "sent"
    assert len(notifier.pushed) == 1
    assert source_url in notifier.pushed[0]
    assert len(notifier.pushed[0].encode("utf-8")) <= RENDER_BYTE_BUDGET


def test_briefing_run_passes_fixed_policy_order_to_generation(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """The briefing model receives the locally decided P0-before-P3 evidence order."""
    _seed_source(session_factory, "policy-source", category="telecom", priority=100)
    _seed_source(session_factory, "mobile-source", category="telecom", priority=10)
    collector = FakeRSSCollector(
        {
            "policy-source": [
                _candidate(
                    "policy-source",
                    "policy",
                    title="工信部发布通信规划",
                    content_text="通信网络专项政策文件" * 10,
                )
            ],
            "mobile-source": [
                _candidate(
                    "mobile-source",
                    "mobile",
                    title="中国移动启动基站集采",
                    content_text="无线网建设项目启动" * 10,
                )
            ],
        }
    )
    llm = FakeBriefingLLM(
        {
            "通信行业日报": _llm_payload(
                "https://mobile-source.example.test/mobile", "来源 mobile-source"
            )
        }
    )
    service = _build_service(
        session_factory,
        tmp_path / "briefings",
        collector=collector,
        llm=llm,
        notifier=None,
    )

    report = asyncio.run(service.run())

    assert {entry.category: entry.status for entry in report.categories} == {
        "telecom": "generated",
        "ai": "skipped",
    }
    telecom_prompt = llm.user_prompts[0]
    assert telecom_prompt.index("中国移动启动基站集采") < telecom_prompt.index("工信部发布通信规划")
    assert "已确定优先级：P0" in telecom_prompt
    assert "已确定优先级：P3" in telecom_prompt


def test_briefing_run_ignores_historical_sources_absent_from_current_policy(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """Removing a source from the current YAML policy takes effect without mutating its row."""
    _seed_source(session_factory, "curated-ai", category="ai")
    _seed_source(session_factory, "retired-general-feed", category="ai")
    collector = FakeRSSCollector(
        {
            "curated-ai": [_candidate("curated-ai", "curated")],
            "retired-general-feed": [_candidate("retired-general-feed", "retired")],
        }
    )
    service = _build_service(
        session_factory,
        tmp_path / "briefings",
        collector=collector,
        llm=FakeBriefingLLM(
            {"AI 动态日报": _llm_payload("https://curated-ai.example.test/curated", "精选来源")}
        ),
        notifier=None,
        briefing_source_ids=frozenset({"curated-ai"}),
    )

    report = asyncio.run(service.run())

    assert collector.collected_source_ids == ["curated-ai"]
    assert {entry.category: entry.status for entry in report.categories} == {
        "telecom": "skipped",
        "ai": "generated",
    }


def test_briefing_run_uses_evidence_fallback_when_model_fails(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """A model outage still ships an evidence-backed briefing for that category."""
    _seed_source(session_factory, "telecom-source", category="telecom")
    _seed_source(session_factory, "ai-source", category="ai")
    collector = FakeRSSCollector(
        {
            "telecom-source": [_candidate("telecom-source", "t1")],
            "ai-source": [_candidate("ai-source", "a1")],
        }
    )
    llm = FakeBriefingLLM(
        {
            "通信行业日报": RuntimeError("llm unavailable"),
            "AI 动态日报": _llm_payload("https://ai-source.example.test/a1", "来源 ai-source"),
        }
    )
    output_dir = tmp_path / "briefings"
    notifier = RecordingNotifier()
    service = _build_service(
        session_factory, output_dir, collector=collector, llm=llm, notifier=notifier
    )

    report = asyncio.run(service.run())

    by_category = {entry.category: entry for entry in report.categories}
    assert by_category["telecom"].status == "generated"
    assert by_category["telecom"].push_status == "sent"
    assert by_category["ai"].status == "generated"
    assert by_category["ai"].push_status == "sent"
    briefings = read_briefings_for_date(output_dir, date.fromisoformat(report.date))
    assert set(briefings) == {"telecom", "ai"}
    assert "入选原因" in briefings["telecom"]
    assert "https://telecom-source.example.test/t1" in briefings["telecom"]
    assert len(notifier.pushed) == 2


def test_briefing_run_skips_a_category_without_eligible_articles(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """A category with no qualifying articles is skipped with no file or LLM call."""
    _seed_source(session_factory, "telecom-source", category="telecom")
    _seed_source(session_factory, "ai-source", category="ai")
    collector = FakeRSSCollector({"telecom-source": [_candidate("telecom-source", "t1")]})
    llm = FakeBriefingLLM(
        {
            "通信行业日报": _llm_payload(
                "https://telecom-source.example.test/t1", "来源 telecom-source"
            )
        }
    )
    output_dir = tmp_path / "briefings"
    service = _build_service(
        session_factory, output_dir, collector=collector, llm=llm, notifier=None
    )

    report = asyncio.run(service.run())

    by_category = {entry.category: entry for entry in report.categories}
    assert by_category["telecom"].status == "generated"
    assert by_category["telecom"].article_count == 1
    assert by_category["ai"].status == "skipped"
    assert by_category["ai"].file_path is None
    assert llm.operations == [LLMOperation.GENERATE_BRIEFING]
    briefings = read_briefings_for_date(output_dir, date.fromisoformat(report.date))
    assert set(briefings) == {"telecom"}


def test_briefing_run_rejects_article_outside_its_24_hour_window(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """Briefing freshness is enforced even if a collector returns an old article."""
    now = datetime(2026, 8, 25, 0, tzinfo=UTC)
    _seed_source(session_factory, "telecom-source", category="telecom")
    collector = FakeRSSCollector(
        {
            "telecom-source": [
                _candidate(
                    "telecom-source",
                    "stale",
                    published_at=now - timedelta(hours=25),
                )
            ]
        }
    )
    llm = FakeBriefingLLM({})
    service = _build_service(
        session_factory,
        tmp_path / "briefings",
        collector=collector,
        llm=llm,
        notifier=None,
        clock=FixedClock(now),
    )

    report = asyncio.run(service.run())

    telecom = next(item for item in report.categories if item.category == "telecom")
    assert telecom.status == "skipped"
    assert telecom.reason == "no_eligible_articles"
    assert llm.operations == []


def test_briefing_run_on_monday_includes_articles_from_the_previous_friday(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """Monday's management briefing covers the complete weekend rather than only Sunday."""
    now = datetime(2026, 8, 24, 0, tzinfo=UTC)
    _seed_source(session_factory, "telecom-source", category="telecom")
    collector = FakeRSSCollector(
        {
            "telecom-source": [
                _candidate(
                    "telecom-source",
                    "friday-network",
                    published_at=datetime(2026, 8, 20, 16, 30, tzinfo=UTC),
                )
            ]
        }
    )
    llm = FakeBriefingLLM(
        {
            "通信行业日报": _llm_payload(
                "https://telecom-source.example.test/friday-network", "来源 telecom-source"
            )
        }
    )
    service = _build_service(
        session_factory,
        tmp_path / "briefings",
        collector=collector,
        llm=llm,
        notifier=None,
        clock=FixedClock(now),
    )

    report = asyncio.run(service.run())

    telecom = next(item for item in report.categories if item.category == "telecom")
    assert collector.collection_windows[0] == CollectionWindow(
        start=datetime(2026, 8, 20, 16, tzinfo=UTC), end=now
    )
    assert telecom.status == "generated"


def test_briefing_run_drops_an_article_whose_source_page_cannot_be_opened(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """Feed text alone must not allow a broken reader-facing source URL into WeCom."""
    _seed_source(session_factory, "telecom-source", category="telecom")
    collector = FakeRSSCollector({"telecom-source": [_candidate("telecom-source", "broken-link")]})
    service = _build_service(
        session_factory,
        tmp_path / "briefings",
        collector=collector,
        llm=FakeBriefingLLM({}),
        notifier=None,
        extractor=UnreachableLinkExtractor(),
    )

    report = asyncio.run(service.run())

    telecom = next(item for item in report.categories if item.category == "telecom")
    assert telecom.status == "skipped"
    assert telecom.reason == "no_eligible_articles"


def test_briefing_run_rejects_stale_date_found_during_body_extraction(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """An extracted publication date replaces the temporary discovery timestamp."""
    _seed_source(session_factory, "telecom-source", category="telecom")
    collector = FakeRSSCollector(
        {
            "telecom-source": [
                ArticleCandidate(
                    source_id="telecom-source",
                    url="https://telecom-source.example.test/extracted-stale",
                    title="中国移动历史业绩公告",
                )
            ]
        }
    )
    now = datetime(2026, 8, 21, 9, tzinfo=UTC)
    llm = FakeBriefingLLM(
        {
            "通信行业日报": _llm_payload(
                "https://telecom-source.example.test/extracted-stale", "来源 telecom-source"
            )
        }
    )
    service = _build_service(
        session_factory,
        tmp_path / "briefings",
        collector=collector,
        llm=llm,
        notifier=None,
        extractor=FakeExtractor(
            "已提取正文。" * 100,
            published_at=now - timedelta(days=8),
        ),
        clock=FixedClock(now),
    )

    report = asyncio.run(service.run())

    telecom = next(item for item in report.categories if item.category == "telecom")
    assert telecom.status == "skipped"
    assert telecom.reason == "no_eligible_articles"
    assert llm.operations == []


def test_briefing_run_rejects_article_without_verified_publication_date(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """A discovery timestamp must never stand in for a briefing publication time."""
    _seed_source(session_factory, "telecom-source", category="telecom")
    collector = FakeRSSCollector(
        {
            "telecom-source": [
                ArticleCandidate(
                    source_id="telecom-source",
                    url="https://telecom-source.example.test/undated",
                    title="中国移动未标日期公告",
                )
            ]
        }
    )
    now = datetime(2026, 8, 21, 9, tzinfo=UTC)
    llm = FakeBriefingLLM(
        {
            "通信行业日报": _llm_payload(
                "https://telecom-source.example.test/undated", "来源 telecom-source"
            )
        }
    )
    service = _build_service(
        session_factory,
        tmp_path / "briefings",
        collector=collector,
        llm=llm,
        notifier=None,
        extractor=FakeExtractor("已提取正文。" * 100),
        clock=FixedClock(now),
    )

    report = asyncio.run(service.run())

    telecom = next(item for item in report.categories if item.category == "telecom")
    assert telecom.status == "skipped"
    assert telecom.reason == "no_eligible_articles"
    assert llm.operations == []


def _two_category_setup(
    session_factory: sessionmaker[Session],
) -> tuple[FakeRSSCollector, FakeBriefingLLM]:
    """Seed one telecom and one ai source with matching canned LLM payloads."""
    _seed_source(session_factory, "telecom-source", category="telecom")
    _seed_source(session_factory, "ai-source", category="ai")
    collector = FakeRSSCollector(
        {
            "telecom-source": [_candidate("telecom-source", "t1")],
            "ai-source": [_candidate("ai-source", "a1")],
        }
    )
    llm = FakeBriefingLLM(
        {
            "通信行业日报": _llm_payload(
                "https://telecom-source.example.test/t1", "来源 telecom-source"
            ),
            "AI 动态日报": _llm_payload("https://ai-source.example.test/a1", "来源 ai-source"),
        }
    )
    return collector, llm


def test_second_non_force_run_skips_completed_categories(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """A completed category is neither regenerated nor pushed again on the same day."""
    collector, llm = _two_category_setup(session_factory)
    notifier = RecordingNotifier()
    output_dir = tmp_path / "briefings"
    service = _build_service(
        session_factory, output_dir, collector=collector, llm=llm, notifier=notifier
    )
    asyncio.run(service.run())

    report = asyncio.run(service.run())

    assert all(entry.status == "skipped" for entry in report.categories)
    assert all(entry.reason == ALREADY_COMPLETED for entry in report.categories)
    assert llm.operations == [LLMOperation.GENERATE_BRIEFING, LLMOperation.GENERATE_BRIEFING]
    assert len(notifier.pushed) == 2


def test_force_run_regenerates_despite_completion_markers(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """A forced rerun ignores the per-day completion markers."""
    collector, llm = _two_category_setup(session_factory)
    notifier = RecordingNotifier()
    output_dir = tmp_path / "briefings"
    service = _build_service(
        session_factory, output_dir, collector=collector, llm=llm, notifier=notifier
    )
    asyncio.run(service.run())

    report = asyncio.run(service.run(force=True))

    assert all(entry.status == "generated" for entry in report.categories)
    assert len(llm.operations) == 4
    assert len(notifier.pushed) == 4


def test_failed_push_leaves_no_marker_so_the_next_run_retries(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """A push failure must not mark the category done; the next run regenerates it."""
    collector, llm = _two_category_setup(session_factory)
    notifier = FailingNotifier()
    output_dir = tmp_path / "briefings"
    service = _build_service(
        session_factory, output_dir, collector=collector, llm=llm, notifier=notifier
    )
    first_report = asyncio.run(service.run())

    assert all(entry.push_status == "failed" for entry in first_report.categories)
    assert not list(output_dir.glob("*.done"))

    second_report = asyncio.run(service.run())

    assert all(entry.status == "generated" for entry in second_report.categories)
    assert len(llm.operations) == 4
    assert notifier.attempts == 4


def test_push_test_sends_one_timestamped_markdown_without_running_the_pipeline(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """The manual test trigger exercises only the push channel, never sources or the LLM."""
    llm = FakeBriefingLLM({})
    notifier = RecordingNotifier()
    service = _build_service(
        session_factory,
        tmp_path / "briefings",
        collector=FakeRSSCollector({}),
        llm=llm,
        notifier=notifier,
    )

    push_status = asyncio.run(service.push_test())

    assert push_status == "sent"
    assert len(notifier.pushed) == 1
    markdown = notifier.pushed[0]
    assert markdown.startswith("# DailyCast 简报推送测试")
    assert "触发时间：" in markdown
    assert "通信行业日报" in markdown
    assert "AI 动态日报" in markdown
    assert llm.operations == []
    assert not (tmp_path / "briefings").exists()


def test_push_test_reports_disabled_without_a_configured_webhook(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """A deployment without a push target answers disabled instead of failing."""
    service = _build_service(
        session_factory,
        tmp_path / "briefings",
        collector=FakeRSSCollector({}),
        llm=FakeBriefingLLM({}),
        notifier=None,
    )

    assert asyncio.run(service.push_test()) == "disabled"


def test_push_test_propagates_push_failures_for_direct_debugging(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """A broken webhook surfaces its error from the test trigger instead of a report."""
    notifier = FailingNotifier()
    service = _build_service(
        session_factory,
        tmp_path / "briefings",
        collector=FakeRSSCollector({}),
        llm=FakeBriefingLLM({}),
        notifier=notifier,
    )

    with pytest.raises(RuntimeError, match="webhook down"):
        asyncio.run(service.push_test())
    assert notifier.attempts == 1


def test_concurrent_run_raises_briefing_run_in_progress(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """A second overlapping run fails fast instead of duplicating collection and LLM work."""
    collector, llm = _two_category_setup(session_factory)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_generate = llm.generate_structured

    async def blocking_generate(*args: object, **kwargs: object) -> StructuredResult:
        entered.set()
        await release.wait()
        return await original_generate(*args, **kwargs)  # type: ignore[arg-type]

    llm.generate_structured = blocking_generate  # type: ignore[method-assign]
    output_dir = tmp_path / "briefings"
    service = _build_service(
        session_factory, output_dir, collector=collector, llm=llm, notifier=None
    )

    async def scenario() -> None:
        first = asyncio.create_task(service.run())
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert service.run_in_progress is True
        with pytest.raises(BriefingRunInProgressError, match="already in progress"):
            await service.run()
        release.set()
        report = await first
        assert all(entry.status == "generated" for entry in report.categories)

    asyncio.run(scenario())
    assert service.run_in_progress is False


def test_exhausted_llm_budget_uses_evidence_fallback_without_provider_calls(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """A depleted budget still ships evidence-backed briefings without an LLM call."""
    collector, llm = _two_category_setup(session_factory)
    output_dir = tmp_path / "briefings"
    service = _build_service(
        session_factory,
        output_dir,
        collector=collector,
        llm=llm,
        notifier=None,
        budget_factory=lambda: BudgetController(max_calls=0),
    )

    report = asyncio.run(service.run())

    assert all(entry.status == "generated" for entry in report.categories)
    assert llm.operations == []
    assert set(read_briefings_for_date(output_dir, date.fromisoformat(report.date))) == {
        "telecom",
        "ai",
    }


def test_llm_budget_reservations_accumulate_per_run(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """Each run reserves one call per generated category against a fresh budget."""
    collector, llm = _two_category_setup(session_factory)
    budgets: list[BudgetController] = []

    def factory() -> BudgetController:
        budget = BudgetController()
        budgets.append(budget)
        return budget

    output_dir = tmp_path / "briefings"
    service = _build_service(
        session_factory,
        output_dir,
        collector=collector,
        llm=llm,
        notifier=None,
        budget_factory=factory,
    )

    asyncio.run(service.run())

    assert len(budgets) == 1
    assert budgets[0].call_count == 2
    assert budgets[0].input_tokens > 0
    assert budgets[0].output_tokens == 2 * llm.max_output_tokens


class StubProvider:
    """One configurable provider leg for failover budget tests."""

    def __init__(
        self,
        *,
        name: str,
        max_output_tokens: int,
        payload: dict[str, object] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.provider_name = name
        self.model = f"{name}-model"
        self.max_output_tokens = max_output_tokens
        self._payload = payload
        self._error = error
        self.calls = 0
        self.attempt_messages: list[Sequence[LLMMessage]] = []

    def generation_config_hash(self, model_options: Mapping[str, object]) -> str:
        """Return a stable per-leg identity for the failover contract."""
        del model_options
        return f"{self.provider_name}-config"

    async def generate_structured(
        self,
        operation: LLMOperation,
        messages: Sequence[LLMMessage],
        response_schema: type[BaseModel],
        model_options: Mapping[str, object],
    ) -> StructuredResult:
        """Record the attempt, then fail or succeed as configured."""
        del operation, response_schema, model_options
        self.calls += 1
        self.attempt_messages.append(messages)
        if self._error is not None:
            raise self._error
        assert self._payload is not None
        return StructuredResult(
            content=self._payload,  # type: ignore[arg-type]
            model=self.model,
            usage=LLMUsage(input_tokens=5, output_tokens=5),
            request_id=f"{self.provider_name}-1",
        )


def _telecom_only_setup(session_factory: sessionmaker[Session]) -> FakeRSSCollector:
    """Seed one telecom source so exactly one category reaches the LLM."""
    _seed_source(session_factory, "telecom-source", category="telecom")
    return FakeRSSCollector({"telecom-source": [_candidate("telecom-source", "t1")]})


def _telecom_payload() -> dict[str, object]:
    return _llm_payload("https://telecom-source.example.test/t1", "来源 telecom-source")


def _recording_budget_factory(
    budgets: list[BudgetController], **limits: int
) -> Callable[[], BudgetController]:
    """Capture each run's budget for reservation assertions."""

    def factory() -> BudgetController:
        budget = BudgetController(**limits)
        budgets.append(budget)
        return budget

    return factory


def test_failover_uses_evidence_fallback_when_budget_prevents_second_attempt(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """With one call left, fallback stays uncalled and evidence still reaches readers."""
    collector = _telecom_only_setup(session_factory)
    primary = StubProvider(name="primary", max_output_tokens=100, error=LLMProviderError())
    fallback = StubProvider(name="fallback", max_output_tokens=200, payload=_telecom_payload())
    budgets: list[BudgetController] = []
    service = _build_service(
        session_factory,
        tmp_path / "briefings",
        collector=collector,
        llm=FailoverLLMProvider(primary, fallback),
        notifier=None,
        budget_factory=_recording_budget_factory(budgets, max_calls=1),
    )

    report = asyncio.run(service.run())

    by_category = {entry.category: entry for entry in report.categories}
    assert by_category["telecom"].status == "generated"
    assert primary.calls == 1
    assert fallback.calls == 0
    assert budgets[0].call_count == 1


def test_failover_primary_success_reserves_exactly_once(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """A successful primary costs one reservation, so max_calls=1 still generates."""
    collector = _telecom_only_setup(session_factory)
    primary = StubProvider(name="primary", max_output_tokens=100, payload=_telecom_payload())
    fallback = StubProvider(name="fallback", max_output_tokens=200, payload=_telecom_payload())
    budgets: list[BudgetController] = []
    service = _build_service(
        session_factory,
        tmp_path / "briefings",
        collector=collector,
        llm=FailoverLLMProvider(primary, fallback),
        notifier=None,
        budget_factory=_recording_budget_factory(budgets, max_calls=1),
    )

    report = asyncio.run(service.run())

    by_category = {entry.category: entry for entry in report.categories}
    assert by_category["telecom"].status == "generated"
    assert primary.calls == 1
    assert fallback.calls == 0
    assert budgets[0].call_count == 1
    assert budgets[0].output_tokens == 100


def test_failover_reserves_each_attempt_with_its_own_output_allowance(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """A failed primary plus a successful fallback costs two distinct reservations."""
    collector = _telecom_only_setup(session_factory)
    primary = StubProvider(name="primary", max_output_tokens=100, error=LLMProviderError())
    fallback = StubProvider(name="fallback", max_output_tokens=200, payload=_telecom_payload())
    budgets: list[BudgetController] = []
    service = _build_service(
        session_factory,
        tmp_path / "briefings",
        collector=collector,
        llm=FailoverLLMProvider(primary, fallback),
        notifier=None,
        budget_factory=_recording_budget_factory(budgets, max_calls=2),
    )

    report = asyncio.run(service.run())

    by_category = {entry.category: entry for entry in report.categories}
    assert by_category["telecom"].status == "generated"
    assert primary.calls == 1
    assert fallback.calls == 1
    assert budgets[0].call_count == 2
    assert budgets[0].output_tokens == 100 + 200
    expected_input = sum(
        estimate_message_input_tokens(messages)
        for messages in primary.attempt_messages + fallback.attempt_messages
    )
    assert budgets[0].input_tokens == expected_input


def test_failover_double_failure_still_reserves_both_attempts_before_evidence_fallback(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """Two failed attempts consume their budget before the evidence fallback is rendered."""
    collector = _telecom_only_setup(session_factory)
    primary = StubProvider(name="primary", max_output_tokens=100, error=LLMProviderError())
    fallback = StubProvider(name="fallback", max_output_tokens=200, error=LLMProviderError())
    budgets: list[BudgetController] = []
    service = _build_service(
        session_factory,
        tmp_path / "briefings",
        collector=collector,
        llm=FailoverLLMProvider(primary, fallback),
        notifier=None,
        budget_factory=_recording_budget_factory(budgets, max_calls=2),
    )

    report = asyncio.run(service.run())

    by_category = {entry.category: entry for entry in report.categories}
    assert by_category["telecom"].status == "generated"
    assert primary.calls == 1
    assert fallback.calls == 1
    assert budgets[0].call_count == 2


def test_create_run_task_reserves_the_slot_within_one_event_loop_tick(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """A second trigger in the same tick fails before the first task has even started."""
    collector, llm = _two_category_setup(session_factory)
    service = _build_service(
        session_factory, tmp_path / "briefings", collector=collector, llm=llm, notifier=None
    )

    async def scenario() -> None:
        first = service.create_run_task()
        with pytest.raises(BriefingRunInProgressError, match="already in progress"):
            service.create_run_task()
        with pytest.raises(BriefingRunInProgressError, match="already in progress"):
            await service.run()
        report = await first
        assert all(entry.status == "generated" for entry in report.categories)
        # The slot is released once the first run finishes, so another run may start.
        second_report = await service.run()
        assert all(entry.status == "skipped" for entry in second_report.categories)

    asyncio.run(scenario())
    assert service.run_in_progress is False
