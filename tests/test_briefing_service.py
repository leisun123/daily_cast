"""Briefing service end-to-end tests with a real database and fake network edges."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from editorial_test_support import upgraded_session_factory
from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker

from dailycast.briefing.service import BriefingService, read_briefings_for_date
from dailycast.briefing.wecom import WeComNotifier
from dailycast.db.models import LLMOperation, Source, SourceKind
from dailycast.db.repositories import SourceRepository
from dailycast.db.transactions import UnitOfWork
from dailycast.llm.contracts import LLMMessage, LLMUsage, StructuredResult
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


class RecordingNotifier(WeComNotifier):
    """Capture pushed markdown without a webhook."""

    def __init__(self) -> None:
        self.pushed: list[str] = []

    async def push(self, markdown: str) -> None:
        """Record the exact markdown that would have been sent to WeCom."""
        self.pushed.append(markdown)


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
    llm: FakeBriefingLLM,
    notifier: WeComNotifier | None,
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
