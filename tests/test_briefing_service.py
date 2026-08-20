"""Briefing service end-to-end tests with a real database and fake network edges."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from editorial_test_support import upgraded_session_factory
from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker

from dailycast.briefing.service import (
    ALREADY_COMPLETED,
    BriefingRunInProgressError,
    BriefingService,
    read_briefings_for_date,
)
from dailycast.briefing.webhook import WebhookNotifier
from dailycast.core.errors import LLMProviderError
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
)
from dailycast.sources.extraction import ContentExtractor, FetchPolicy
from dailycast.sources.service import ArticleService, SourceCollectionService


class FakeRSSCollector:
    """Return canned candidates per source without any network access."""

    def __init__(self, candidates_by_source: Mapping[str, Sequence[ArticleCandidate]]) -> None:
        self._candidates_by_source = candidates_by_source
        self.collected_source_ids: list[str] = []

    async def collect(self, source: Source, window: CollectionWindow) -> CollectionResult:
        """Record which sources the briefing flow actually asked to collect."""
        del window
        self.collected_source_ids.append(source.id)
        return CollectionResult(
            source_id=source.id,
            candidates=tuple(self._candidates_by_source.get(source.id, ())),
        )


class FakeExtractor(ContentExtractor):
    """Return a canned body for any article that still lacks one."""

    def __init__(self, content: str) -> None:
        self._content = content

    async def extract(self, url: str, policy: FetchPolicy) -> ExtractedArticle:
        """Pretend every fetch succeeded so tests never touch the network."""
        del policy
        return ExtractedArticle(
            requested_url=url,
            final_url=url,
            content_text=self._content,
            http_status=200,
            fetched_at=datetime.now(UTC),
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


def _candidate(source_id: str, key: str) -> ArticleCandidate:
    return ArticleCandidate(
        source_id=source_id,
        url=f"https://news.example.test/{key}",
        title=f"新闻 {key}",
        content_text=f"这是 {key} 的正文内容，足够通过长度过滤。" * 3,
        published_at=datetime.now(UTC),
    )


def _llm_payload(source_url: str, source_name: str) -> dict[str, object]:
    return {
        "overview": "今天该类目整体平稳，值得关注以下几件事。",
        "items": [
            {
                "headline": "一句话头条",
                "summary": "第一句摘要。第二句摘要。",
                "source_name": source_name,
                "source_url": source_url,
            }
        ],
    }


def _build_service(
    factory: sessionmaker[Session],
    output_dir: Path,
    *,
    collector: FakeRSSCollector,
    llm: LLMProvider,
    notifier: WebhookNotifier | None,
    budget_factory: Callable[[], BudgetController] = BudgetController,
) -> BriefingService:
    article_service = ArticleService(factory)
    collection_service = SourceCollectionService(
        factory, {SourceKind.RSS: collector}, article_service
    )
    news_processor = NewsProcessor(
        factory,
        ProcessingPolicy(max_age_hours=72, min_content_length=10, similarity_threshold=0.58),
    )
    return BriefingService(
        factory,
        collection_service,
        article_service,
        FakeExtractor("抓取的正文内容。" * 10),
        news_processor,
        llm,
        notifier,
        output_dir=output_dir,
        budget_factory=budget_factory,
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
            "telecom-source": [_candidate("telecom-source", "t1")],
            "ai-source": [_candidate("ai-source", "a1")],
            "podcast-source": [_candidate("podcast-source", "p1")],
        }
    )
    llm = FakeBriefingLLM(
        {
            "通信行业日报": _llm_payload("https://news.example.test/t1", "来源 telecom-source"),
            "AI 动态日报": _llm_payload("https://news.example.test/a1", "来源 ai-source"),
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
    assert "https://news.example.test/t1" in briefings["telecom"]
    assert "https://news.example.test/a1" in briefings["ai"]


def test_briefing_run_isolates_one_category_failure(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """An LLM failure in one category must not block the other category's briefing."""
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
            "AI 动态日报": _llm_payload("https://news.example.test/a1", "来源 ai-source"),
        }
    )
    output_dir = tmp_path / "briefings"
    service = _build_service(
        session_factory, output_dir, collector=collector, llm=llm, notifier=None
    )

    report = asyncio.run(service.run())

    by_category = {entry.category: entry for entry in report.categories}
    assert by_category["telecom"].status == "failed"
    assert by_category["telecom"].error == "llm unavailable"
    assert by_category["ai"].status == "generated"
    assert by_category["ai"].push_status == "disabled"
    briefings = read_briefings_for_date(output_dir, date.fromisoformat(report.date))
    assert set(briefings) == {"ai"}


def test_briefing_run_skips_a_category_without_eligible_articles(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """A category with no qualifying articles is skipped with no file or LLM call."""
    _seed_source(session_factory, "telecom-source", category="telecom")
    _seed_source(session_factory, "ai-source", category="ai")
    collector = FakeRSSCollector({"telecom-source": [_candidate("telecom-source", "t1")]})
    llm = FakeBriefingLLM(
        {"通信行业日报": _llm_payload("https://news.example.test/t1", "来源 telecom-source")}
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
            "通信行业日报": _llm_payload("https://news.example.test/t1", "来源 telecom-source"),
            "AI 动态日报": _llm_payload("https://news.example.test/a1", "来源 ai-source"),
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


def test_exhausted_llm_budget_fails_categories_before_any_provider_call(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """A depleted budget fails every category without invoking the LLM provider."""
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

    assert all(entry.status == "failed" for entry in report.categories)
    assert all("budget" in (entry.error or "") for entry in report.categories)
    assert llm.operations == []


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
    return _llm_payload("https://news.example.test/t1", "来源 telecom-source")


def _recording_budget_factory(
    budgets: list[BudgetController], **limits: int
) -> Callable[[], BudgetController]:
    """Capture each run's budget for reservation assertions."""

    def factory() -> BudgetController:
        budget = BudgetController(**limits)
        budgets.append(budget)
        return budget

    return factory


def test_failover_fallback_attempt_is_not_made_when_budget_is_exhausted(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """With one call left, a failed primary must not trigger the fallback attempt."""
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
    assert by_category["telecom"].status == "failed"
    assert "budget" in (by_category["telecom"].error or "")
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


def test_failover_double_failure_still_reserves_both_attempts(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """Two failed attempts both consume budget before the category is marked failed."""
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
    assert by_category["telecom"].status == "failed"
    assert "budget" not in (by_category["telecom"].error or "")
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
