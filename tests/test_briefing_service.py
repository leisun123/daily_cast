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
from dailycast.briefing.schemas import BriefingEvidence, BriefingItem, BriefingResult
from dailycast.briefing.selection import (
    BriefingSelectionPolicy,
    RankedBriefingEvidence,
    load_selection_policy,
)
from dailycast.briefing.service import (
    ALREADY_COMPLETED,
    ALREADY_PREPARED,
    BriefingRunInProgressError,
    BriefingService,
    _audit_generated_result,
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
        self.focus_prompts: list[str] = []

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
        user_content = messages[-1].content
        if "最终「昨日关注」" in user_content:
            self.focus_prompts.append(user_content)
            focus = self._results_by_marker.get(
                "最终「昨日关注」", {"focus": "通信与AI行业多项进展同步推进。"}
            )
            if isinstance(focus, Exception):
                raise focus
            return StructuredResult(
                content=focus,  # type: ignore[arg-type]
                model=self.model,
                usage=LLMUsage(input_tokens=10, output_tokens=20),
                request_id="fake-briefing-focus",
            )
        self.operations.append(operation)
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


class SequencedBriefingLLM(FakeBriefingLLM):
    """Return successive category responses so selection repair is exercised end to end."""

    def __init__(self, category_marker: str, payloads: Sequence[dict[str, object]]) -> None:
        super().__init__({})
        self._category_marker = category_marker
        self._payloads = list(payloads)

    async def generate_structured(
        self,
        operation: LLMOperation,
        messages: Sequence[LLMMessage],
        response_schema: type[BaseModel],
        model_options: Mapping[str, object],
    ) -> StructuredResult:
        user_content = messages[-1].content
        if "最终「昨日关注」" in user_content:
            return await super().generate_structured(
                operation, messages, response_schema, model_options
            )
        self.operations.append(operation)
        self.user_prompts.append(user_content)
        if self._category_marker not in user_content or not self._payloads:
            raise AssertionError("no sequenced LLM result matched the briefing prompt")
        return StructuredResult(
            content=self._payloads.pop(0),
            model=self.model,
            usage=LLMUsage(input_tokens=10, output_tokens=20),
            request_id="fake-briefing-sequence",
        )


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
        published_at=published_at or datetime.now(UTC) - timedelta(days=1),
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


def test_generated_briefing_audit_fills_omitted_verified_evidence() -> None:
    """A short or partly hallucinated model response cannot silently shrink a briefing."""
    published_at = datetime(2026, 8, 24, 8, tzinfo=UTC)
    evidence = tuple(
        RankedBriefingEvidence(
            evidence=BriefingEvidence(
                title=f"中国移动网络建设进展 {index}",
                source_name=f"来源 {index}",
                published_at=published_at,
                excerpt=f"第 {index} 个已核验原文的完整事实。",
                source_url=f"https://source-{index}.example.test/article",
            ),
            tier="P0",
            specificity=500,
            reason="中国移动直接动态",
            rule_id="telecom-china-mobile",
            source_id=f"source-{index}",
            source_priority=100,
            discovered_at=published_at,
            article_id=index,
        )
        for index in range(1, 6)
    )
    result = BriefingResult(
        overview="今日重点聚焦网络建设。",
        items=[
            BriefingItem(
                headline="模型已选中的第一条",
                summary="第一条已由模型完成概述。",
                why_it_matters="它反映网络建设进展。",
                source_name="来源 1",
                source_url="https://source-1.example.test/article",
            ),
            BriefingItem(
                headline="不可验证条目",
                summary="这个链接不应进入最终消息。",
                why_it_matters="没有对应原文。",
                source_name="来源 9",
                source_url="https://fabricated.example.test/article",
            ),
        ],
    )

    audited = _audit_generated_result(result, evidence)

    assert len(audited.items) == 5
    assert [item.source_url for item in audited.items] == [
        entry.evidence.source_url for entry in evidence
    ]
    assert "https://fabricated.example.test/article" not in {
        item.source_url for item in audited.items
    }
    assert all("…" not in item.summary for item in audited.items)


def test_editorial_audit_does_not_select_an_omitted_candidate() -> None:
    """Only the LLM may decide an editorial candidate is management-relevant."""
    published_at = datetime(2026, 8, 24, 8, tzinfo=UTC)
    evidence = tuple(
        RankedBriefingEvidence(
            evidence=BriefingEvidence(
                title=f"已核验候选 {index}",
                source_name=f"来源 {index}",
                published_at=published_at,
                excerpt=f"第 {index} 个已核验候选。",
                source_url=f"https://source-{index}.example.test/article",
            ),
            tier="LLM",
            specificity=0,
            reason="交由编辑模型判断管理价值",
            rule_id="editorial-llm",
            source_id=f"source-{index}",
            source_priority=100,
            discovered_at=published_at,
            article_id=index,
        )
        for index in range(1, 4)
    )
    result = BriefingResult.model_validate(
        _llm_payload("https://source-1.example.test/article", "来源 1")
    )

    audited = _audit_generated_result(result, evidence, allow_evidence_backfill=False)

    assert [item.source_url for item in audited.items] == [
        "https://source-1.example.test/article"
    ]


def test_generated_briefing_audit_limits_one_ai_publisher_and_fills_alternatives() -> None:
    """A model cannot turn a balanced AI pool into a half-single-outlet final list."""
    published_at = datetime(2026, 8, 25, 8, tzinfo=UTC)
    domains = ["leiphone.com"] * 4 + [
        "ithome.com",
        "thepaper.cn",
        "36kr.com",
        "jiemian.com",
    ]
    evidence = tuple(
        RankedBriefingEvidence(
            evidence=BriefingEvidence(
                title=f"全球 AI 产业进展 {index}",
                source_name=f"来源 {index}",
                published_at=published_at,
                excerpt=f"第 {index} 个已核验事实。",
                source_url=f"https://{domain}/article-{index}",
            ),
            tier="LLM",
            specificity=0,
            reason="已通过时间、正文和原文链接核验",
            rule_id="editorial-llm",
            source_id=f"source-{index}",
            source_priority=100,
            discovered_at=published_at,
            article_id=index,
        )
        for index, domain in enumerate(domains, start=1)
    )
    selected_urls = [entry.evidence.source_url for entry in evidence[:6]]
    result = BriefingResult.model_validate(_llm_payloads(selected_urls, "AI 中文来源"))

    audited = _audit_generated_result(
        result,
        evidence,
        publisher_cap=2,
        fallback_publisher_cap=6,
    )

    assert len(audited.items) == 6
    assert sum("leiphone.com" in item.source_url for item in audited.items) == 2
    assert any("36kr.com" in item.source_url for item in audited.items)
    assert any("jiemian.com" in item.source_url for item in audited.items)


def test_generated_briefing_audit_drops_an_omitted_long_raw_title() -> None:
    """A filler may not turn a publisher's long headline into a broken chat title."""
    published_at = datetime(2026, 8, 24, 8, tzinfo=UTC)
    evidence = tuple(
        RankedBriefingEvidence(
            evidence=BriefingEvidence(
                title=(
                    f"中国移动网络建设进展 {index}"
                    if index < 5
                        else (
                            "半年3轮10亿，他们都投了这家已经把机器人卖到500个家庭的公司"
                            "并计划进入更多城市，后续还将拓展海外市场、教育场景和更多家庭用户"
                        )
                ),
                source_name=f"来源 {index}",
                published_at=published_at,
                excerpt=f"第 {index} 个已核验原文的完整事实。",
                source_url=f"https://source-{index}.example.test/article",
            ),
            tier="P0",
            specificity=500,
            reason="中国移动直接动态",
            rule_id="telecom-china-mobile",
            source_id=f"source-{index}",
            source_priority=100,
            discovered_at=published_at,
            article_id=index,
        )
        for index in range(1, 6)
    )
    result = BriefingResult(
        overview="今日重点聚焦网络建设。",
        items=[
            BriefingItem(
                headline=f"第 {index} 条完整标题",
                summary="模型已完成的完整摘要。",
                why_it_matters="它反映网络建设进展。",
                source_name=f"来源 {index}",
                source_url=f"https://source-{index}.example.test/article",
            )
            for index in range(1, 5)
        ],
    )

    audited = _audit_generated_result(result, evidence)

    assert len(audited.items) == 4
    assert all(item.source_url != "https://source-5.example.test/article" for item in audited.items)
    assert all("…" not in item.headline for item in audited.items)


def test_editorial_audit_keeps_a_complete_model_headline_over_60_characters() -> None:
    """A local aesthetic cap must not overrule the editor's verified selection."""
    published_at = datetime(2026, 8, 24, 8, tzinfo=UTC)
    evidence = (
        RankedBriefingEvidence(
            evidence=BriefingEvidence(
                title="已核验原文标题",
                source_name="来源 1",
                published_at=published_at,
                excerpt="已核验原文事实。",
                source_url="https://source-1.example.test/article",
            ),
            tier="LLM",
            specificity=0,
            reason="交由编辑模型判断",
            rule_id="editorial-llm",
            source_id="source-1",
            source_priority=100,
            discovered_at=published_at,
            article_id=1,
        ),
    )
    result = BriefingResult(
        overview="今日重点。",
        items=[
            BriefingItem(
                headline=(
                    "模型生成了一个超过六十个汉字并且不适合企业微信紧凑标题列表展示的完整长标题，"
                    "还加入了多余的背景、过程、数字与结论来刻意超过上限"
                ),
                summary="模型生成了完整摘要。",
                why_it_matters="它反映行业进展。",
                source_name="来源 1",
                source_url="https://source-1.example.test/article",
            )
        ],
    )

    audited = _audit_generated_result(result, evidence, allow_evidence_backfill=False)

    assert audited.items == result.items


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


def test_briefing_run_persists_categories_but_pushes_one_compact_merged_message(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """Category state stays independent while WeCom receives one compact daily message."""
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
            "最终「昨日关注」": {
                "focus": (
                    "运营商将算力投资、6G战略和网络智能化同步推进；"
                    "国内 AI 侧，国产模型与智能体应用加速。"
                )
            },
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
    assert len(notifier.pushed) == 1
    briefings = read_briefings_for_date(output_dir, date.fromisoformat(report.date))
    assert set(briefings) == {"telecom", "ai", "merged"}
    assert "# 通信行业日报" in briefings["telecom"]
    assert (
        sum(
            f"https://telecom-source.example.test/t{index}" in briefings["telecom"]
            for index in range(1, 6)
        )
            == 5
    )
    assert (
        sum(f"https://ai-source.example.test/a{index}" in briefings["ai"] for index in range(1, 6))
        == 5
    )
    delivered = notifier.pushed[0]
    assert briefings["merged"] == delivered
    assert delivered.startswith("# 【行业观察日报】")
    assert "## 📡 通信" in delivered
    assert "## 🤖 AI" in delivered
    assert (
        "## 昨日关注\n> 运营商将算力投资、6G战略和网络智能化同步推进；"
        "国内 AI 侧，国产模型与智能体应用加速。"
        in delivered
    )
    assert "重点动态" not in delivered
    assert len(llm.focus_prompts) == 1
    assert "发生了什么" not in delivered
    assert "为什么值得看" not in delivered
    assert "https://telecom-source.example.test/" in delivered
    assert "[第 5 条重要进展](https://ai-source.example.test/a5)" in delivered
    assert len(delivered.encode("utf-8")) <= RENDER_BYTE_BUDGET


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


def test_telecom_briefing_passes_the_verified_candidate_pool_to_generation(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """The LLM receives candidates and decides their semantic management relevance."""
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
    assert "中国移动启动基站集采" in telecom_prompt
    assert "工信部发布通信规划" in telecom_prompt
    assert "候选文章（已通过时间、正文与原文链接核验）" in telecom_prompt
    assert "来源配置或关键词命中" in telecom_prompt
    assert "已确定优先级" not in telecom_prompt


def test_ai_briefing_delegates_candidate_selection_to_the_llm(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """A verified AI-source candidate is shown to the editor without a local keyword gate."""
    _seed_source(session_factory, "ai-source", category="ai")
    source_url = "https://ai-source.example.test/editorial-choice"
    collector = FakeRSSCollector(
        {
            "ai-source": [
                _candidate(
                    "ai-source",
                    "editorial-choice",
                    title="自动化客服接入新服务",
                    content_text="机构推出多轮对话服务，已接入软件平台并覆盖三个城市。" * 10,
                )
            ]
        }
    )
    llm = FakeBriefingLLM({"AI 动态日报": _llm_payload(source_url, "来源 ai-source")})
    service = _build_service(
        session_factory,
        tmp_path / "briefings",
        collector=collector,
        llm=llm,
        notifier=None,
    )

    report = asyncio.run(service.run())

    ai_report = next(entry for entry in report.categories if entry.category == "ai")
    assert ai_report.status == "generated"
    assert source_url in llm.user_prompts[0]
    assert "由你自行挑选" in llm.user_prompts[0]
    assert "不得重新挑选" not in llm.user_prompts[0]


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
    assert set(briefings) == {"telecom", "ai", "merged"}
    assert "已核验原文标题降级列表" in briefings["telecom"]
    assert "未生成新闻摘要" in briefings["telecom"]
    assert "入选原因" not in briefings["telecom"]
    assert "https://telecom-source.example.test/t1" in briefings["telecom"]
    assert len(notifier.pushed) == 1


def test_model_outage_sends_six_explicitly_degraded_verified_titles(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """Removing the six-item cap or degraded label would make a model outage unsafe."""
    candidates_by_source: dict[str, list[ArticleCandidate]] = {}
    for index in range(1, 9):
        source_id = f"telecom-{index}"
        _seed_source(session_factory, source_id, category="telecom")
        candidates_by_source[source_id] = [
            _candidate(
                source_id,
                f"item-{index}",
                title=(
                    "第8家运营商完成覆盖多个地市的无线网络建设并公布下一阶段投资与扩容计划"
                    if index == 8
                    else f"第{index}家运营商完成网络建设并公布下一步计划"
                ),
            )
        ]
    notifier = RecordingNotifier()
    service = _build_service(
        session_factory,
        tmp_path / "briefings",
        collector=FakeRSSCollector(candidates_by_source),
        llm=FakeBriefingLLM({"通信行业日报": RuntimeError("llm unavailable")}),
        notifier=notifier,
    )

    report = asyncio.run(service.run())

    telecom = next(entry for entry in report.categories if entry.category == "telecom")
    assert telecom.status == "generated"
    assert telecom.push_status == "sent"
    assert len(notifier.pushed) == 1
    delivered = notifier.pushed[0]
    assert "📡 通信（降级版）" in delivered
    assert delivered.count("https://telecom-") == 6
    assert "第8家运营商完成覆盖多个地市的无线网络建设并公布下一阶段投资与扩容计划" in delivered
    assert "入选原因" not in delivered


def test_editorial_generation_repairs_invalid_items_with_a_second_llm_selection(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """Dropping invalid links must trigger LLM repair instead of shrinking the six-item brief."""
    candidates_by_source: dict[str, list[ArticleCandidate]] = {}
    urls: list[str] = []
    for index in range(1, 7):
        source_id = f"telecom-repair-{index}"
        _seed_source(session_factory, source_id, category="telecom")
        candidates_by_source[source_id] = [_candidate(source_id, f"item-{index}")]
        urls.append(f"https://{source_id}.example.test/item-{index}")
    initial = _llm_payloads([*urls[:4], "https://fabricated.example.test/item"], "初选来源")
    repair = _llm_payloads(urls[4:], "补选来源")
    llm = SequencedBriefingLLM("通信行业日报", [initial, repair])
    notifier = RecordingNotifier()
    service = _build_service(
        session_factory,
        tmp_path / "briefings",
        collector=FakeRSSCollector(candidates_by_source),
        llm=llm,
        notifier=notifier,
    )

    report = asyncio.run(service.run())

    telecom = next(entry for entry in report.categories if entry.category == "telecom")
    assert telecom.status == "generated"
    assert telecom.push_status == "sent"
    delivered = notifier.pushed[0]
    assert all(url in delivered for url in urls)
    assert "fabricated.example.test" not in delivered
    assert len(llm.user_prompts) == 2
    assert "还缺 2 条" in llm.user_prompts[1]


def test_editorial_generation_keeps_valid_partial_selection_when_repair_cannot_fill_six(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """A short LLM-selected brief must not degrade into unedited source titles."""
    candidates_by_source: dict[str, list[ArticleCandidate]] = {}
    urls: list[str] = []
    for index in range(1, 7):
        source_id = f"telecom-partial-{index}"
        _seed_source(session_factory, source_id, category="telecom")
        candidates_by_source[source_id] = [_candidate(source_id, f"item-{index}")]
        urls.append(f"https://{source_id}.example.test/item-{index}")
    llm = SequencedBriefingLLM(
        "通信行业日报",
        [
            _llm_payloads(urls[:4], "初选来源"),
            _llm_payload("https://fabricated.example.test/item", "补选来源"),
        ],
    )
    notifier = RecordingNotifier()
    service = _build_service(
        session_factory,
        tmp_path / "briefings",
        collector=FakeRSSCollector(candidates_by_source),
        llm=llm,
        notifier=notifier,
    )

    report = asyncio.run(service.run())

    telecom = next(entry for entry in report.categories if entry.category == "telecom")
    assert telecom.status == "generated"
    delivered = notifier.pushed[0]
    assert "📡 通信（降级版）" not in delivered
    assert all(url in delivered for url in urls[:4])
    assert all(url not in delivered for url in urls[4:])
    assert "fabricated.example.test" not in delivered


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
    assert set(briefings) == {"telecom", "merged"}
    assert "## 昨日关注\n> 通信与AI行业多项进展同步推进。" in briefings["merged"]
    assert len(llm.focus_prompts) == 1


def test_briefing_run_rejects_article_outside_the_previous_calendar_day(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """Briefing freshness is enforced even if a collector returns an old article."""
    now = datetime(2026, 8, 25, 0, 30, tzinfo=UTC)
    _seed_source(session_factory, "telecom-source", category="telecom")
    collector = FakeRSSCollector(
        {
            "telecom-source": [
                _candidate(
                    "telecom-source",
                    "stale",
                    published_at=datetime(2026, 8, 23, 15, 59, tzinfo=UTC),
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


def test_briefing_run_uses_the_previous_shanghai_calendar_day(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """A Tuesday run uses Monday 00:00-24:00 in Shanghai, not a rolling 24 hours."""
    now = datetime(2026, 8, 25, 0, 30, tzinfo=UTC)
    _seed_source(session_factory, "telecom-source", category="telecom")
    collector = FakeRSSCollector(
        {
            "telecom-source": [
                _candidate(
                    "telecom-source",
                    "monday-network",
                    published_at=datetime(2026, 8, 24, 8, tzinfo=UTC),
                )
            ]
        }
    )
    service = _build_service(
        session_factory,
        tmp_path / "briefings",
        collector=collector,
        llm=FakeBriefingLLM({}),
        notifier=None,
        clock=FixedClock(now),
    )

    report = asyncio.run(service.run())

    assert collector.collection_windows[0] == CollectionWindow(
        start=datetime(2026, 8, 23, 16, tzinfo=UTC),
        end=datetime(2026, 8, 24, 16, tzinfo=UTC),
    )
    assert report.date == "2026-08-24"


def test_briefing_run_on_monday_includes_friday_through_sunday(
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
        start=datetime(2026, 8, 20, 16, tzinfo=UTC),
        end=datetime(2026, 8, 23, 16, tzinfo=UTC),
    )
    assert report.date == "2026-08-23"
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
    assert len(notifier.pushed) == 1


def test_prepared_delivery_pushes_saved_markdown_without_recollecting_or_regenerating(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """The delivery tick only sends the report prepared before 08:30."""
    collector, llm = _two_category_setup(session_factory)
    notifier = RecordingNotifier()
    output_dir = tmp_path / "briefings"
    service = _build_service(
        session_factory, output_dir, collector=collector, llm=llm, notifier=notifier
    )

    prepared = asyncio.run(service.prepare())
    collected_before_delivery = list(collector.collected_source_ids)
    generated_before_delivery = list(llm.operations)
    retry = asyncio.run(service.prepare())

    delivered = asyncio.run(service.deliver_prepared())

    assert all(entry.status == "generated" for entry in prepared.categories)
    assert all(entry.reason == ALREADY_PREPARED for entry in retry.categories)
    assert notifier.pushed == [
        read_briefings_for_date(output_dir, date.fromisoformat(prepared.date))["merged"]
    ]
    assert collector.collected_source_ids == collected_before_delivery
    assert llm.operations == generated_before_delivery
    assert all(entry.push_status == "sent" for entry in delivered.categories)
    assert len(list(output_dir.glob("*.done"))) == 2


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
    assert len(notifier.pushed) == 2


def test_failed_push_retries_the_prepared_message_without_regenerating(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """A push failure keeps the saved report ready for another delivery attempt."""
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

    assert all(entry.status == "skipped" for entry in second_report.categories)
    assert all(entry.reason == ALREADY_PREPARED for entry in second_report.categories)
    assert len(llm.operations) == 2
    assert notifier.attempts == 2


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
        "merged",
    }


def test_llm_budget_reservations_accumulate_per_run(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """Each run reserves category calls plus one merged-focus call against a fresh budget."""
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
    assert budgets[0].call_count == 3
    assert budgets[0].input_tokens > 0
    assert budgets[0].output_tokens == 3 * llm.max_output_tokens


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
