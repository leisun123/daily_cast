"""Sprint 4B-1 EventCard construction and AI event-ranking behavior."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from dailycast.core.config import load_settings
from dailycast.core.errors import AIBudgetExceededError
from dailycast.core.hashes import sha256_text
from dailycast.core.time import Clock
from dailycast.db.models import (
    ArticleStatus,
    LLMArtifact,
    LLMOperation,
    NewsEvent,
    NewsEventStatus,
    SourceKind,
    TaskRunStatus,
    TaskType,
    TriggerType,
)
from dailycast.db.repositories import (
    ArticleRepository,
    NewsEventRepository,
    SourceRepository,
    TaskRunRepository,
    TaskStepRepository,
)
from dailycast.db.revision import build_alembic_config
from dailycast.db.session import create_session_factory, create_sqlite_engine
from dailycast.db.transactions import UnitOfWork
from dailycast.llm.artifacts import LLMResponseValidationError
from dailycast.llm.budget import BudgetController
from dailycast.llm.contracts import LLMMessage, LLMUsage, StructuredResult
from dailycast.llm.editorial_service import AIEditorialService
from dailycast.llm.prompts.score_events_v2 import SCORE_EVENTS_V2
from dailycast.pipeline.context import PipelineContext
from dailycast.pipeline.steps.ranking import RankingStep


class FakeLLMProvider:
    """Return one configured score payload while recording real service invocations."""

    provider_name = "fake"
    model = "fake-ranking-model"
    max_output_tokens = 100

    def __init__(self, content: dict[str, object]) -> None:
        self.content = content
        self.calls = 0

    def generation_config_hash(self, model_options: dict[str, object]) -> str:
        """Provide a fixed non-secret identity for deterministic cache tests."""
        del model_options
        return sha256_text("fake-ranking-config-v1")

    async def generate_structured(
        self,
        operation: LLMOperation,
        messages: tuple[LLMMessage, ...],
        response_schema: type[object],
        model_options: dict[str, object],
    ) -> StructuredResult:
        """Return structured JSON without an external model call."""
        del operation, messages, response_schema, model_options
        self.calls += 1
        return StructuredResult(
            content=self.content,
            model=self.model,
            usage=LLMUsage(input_tokens=5, output_tokens=7),
            request_id=f"fake-ranking-{self.calls}",
        )


@pytest.fixture
def migrated_session_factory(app_config_path: Path) -> sessionmaker[Session]:
    """Create an isolated database via the real Alembic upgrade path."""
    settings = load_settings(config_path=app_config_path)
    command.upgrade(
        build_alembic_config(
            ini_path=Path(__file__).resolve().parents[1] / "alembic.ini",
            database_url=settings.database.url,
        ),
        "head",
    )
    engine = create_sqlite_engine(settings.database)
    factory = create_session_factory(engine)
    try:
        yield factory
    finally:
        engine.dispose()


def create_event(
    factory: sessionmaker[Session],
    *,
    key: str,
    source_id: str | None = None,
    title: str | None = None,
    deterministic_score: float = 0.0,
    source_priority: int = 50,
    summary: str = "Short, bounded event summary.",
    content: str = "This full article must not become a ranking input.",
) -> NewsEvent:
    """Persist one candidate event with representative source/article evidence."""
    now = datetime(2026, 7, 22, 12, tzinfo=UTC)
    source_id = source_id or f"source-{key}"
    title = title or f"Event {key}"
    with UnitOfWork(factory) as unit:
        assert unit.session is not None
        SourceRepository(unit.session).create(
            id=source_id,
            name=f"Source {key}",
            kind=SourceKind.RSS,
            entry_url=f"https://{source_id}.example.test/rss",
            normalized_entry_url=f"https://{source_id}.example.test/rss",
            priority=source_priority,
            config_json="{}",
        )
        url = f"https://news.example.test/{key}"
        article = ArticleRepository(unit.session).upsert(
            source_id=source_id,
            external_id=key,
            url=url,
            normalized_url=url,
            url_hash=sha256_text(url),
            title=title,
            normalized_title=title.casefold(),
            title_hash=sha256_text(title.casefold()),
            summary=summary,
            content_text=content,
            content_hash=sha256_text(content),
            language="en",
            published_at=now,
            discovered_at=now,
            status=ArticleStatus.ELIGIBLE,
            metadata_json="{}",
        )
        event = NewsEventRepository(unit.session).create(
            event_key=f"2026-07-22:{key}",
            event_date=now.date(),
            representative_article_id=article.id,
            title=article.title,
            summary=summary,
            status=NewsEventStatus.CANDIDATE,
            first_published_at=now,
            last_published_at=now,
            article_count=1,
            source_count=1,
            deterministic_score=deterministic_score,
            risk_flags_json="[]",
            cluster_algorithm="tfidf_char",
            cluster_version="1",
            cluster_threshold=0.58,
            cluster_signature=sha256_text(key),
        )
        ArticleRepository(unit.session).update(article, news_event_id=event.id)
        return event


def create_task_provenance(factory: sessionmaker[Session]) -> tuple[str, int]:
    """Create a persisted running TaskRun and ranking TaskStep for Artifact provenance."""
    task_run_id = str(uuid4())
    with UnitOfWork(factory) as unit:
        assert unit.session is not None
        task_run = TaskRunRepository(unit.session).create(
            id=task_run_id,
            task_type=TaskType.DAILY_GENERATE,
            business_key=f"ranking-test:{task_run_id}",
            idempotency_key=f"ranking-test:{task_run_id}",
            trigger_type=TriggerType.MANUAL,
            status=TaskRunStatus.RUNNING,
            pipeline_version="ranking-test-v1",
            config_fingerprint="a" * 64,
            config_snapshot_json="{}",
            request_json="{}",
        )
        task_step = TaskStepRepository(unit.session).create(
            task_run_id=task_run.id,
            step_name="ranking",
            step_order=6,
            attempt=1,
            status="running",
            details_json="{}",
        )
        return task_run.id, task_step.id


def artifact_count(factory: sessionmaker[Session]) -> int:
    """Return the count of cacheable successful structured model results."""
    with UnitOfWork(factory) as unit:
        assert unit.session is not None
        count = unit.session.scalar(select(func.count()).select_from(LLMArtifact))
        assert count is not None
        return count


def event_score(event_id: int, *, importance: int, relevance: int) -> dict[str, object]:
    """Build one valid structured model score for an input event identifier."""
    return {
        "event_id": event_id,
        "importance": importance,
        "relevance": relevance,
        "confidence": 80,
        "recommend": True,
        "reason": f"Reason for event {event_id}",
        "risks": ["verify wording"],
    }


def test_score_events_v2_prompt_declares_the_complete_json_object_contract() -> None:
    """JSON-object mode needs an explicit semantic schema when strict output is unavailable."""
    instruction = SCORE_EVENTS_V2.system_instruction

    assert SCORE_EVENTS_V2.version == "score_events_v2"
    assert "`scores`" in instruction
    assert "event_id" in instruction
    assert "recommend" in instruction
    assert "exactly once" in instruction
    assert "high-impact" in instruction
    assert "listener" in instruction
    assert "press releases" in instruction


def test_event_card_is_bounded_and_excludes_full_article_content(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """Ranking cards contain only documented metadata, a short summary, and short evidence."""
    content = "FULL_ARTICLE_SENTINEL " * 500
    event = create_event(migrated_session_factory, key="bounded", content=content)
    service = AIEditorialService(migrated_session_factory, FakeLLMProvider({"scores": []}))

    cards = service.build_event_cards((event.id,))

    assert len(cards) == 1
    card = cards[0]
    assert set(card.model_dump()) == {
        "event_id",
        "title",
        "summary",
        "source_count",
        "source_priority",
        "published_time",
        "representative_source",
        "evidence_snippets",
    }
    assert card.summary == "Short, bounded event summary."
    assert content not in card.model_dump_json()
    assert all(len(snippet) <= 240 for snippet in card.evidence_snippets)


def test_invalid_score_schema_is_rejected_without_creating_an_artifact(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """Scores outside the documented 0-100 range fail local validation before persistence."""
    event = create_event(migrated_session_factory, key="invalid-score")
    task_run_id, task_step_id = create_task_provenance(migrated_session_factory)
    provider = FakeLLMProvider({"scores": [event_score(event.id, importance=101, relevance=50)]})
    service = AIEditorialService(migrated_session_factory, provider)

    with pytest.raises(LLMResponseValidationError):
        asyncio.run(
            service.score_events(
                (event.id,),
                task_run_id=task_run_id,
                task_step_id=task_step_id,
                budget=BudgetController(),
            )
        )

    assert artifact_count(migrated_session_factory) == 0


def test_score_events_reuses_the_exact_llm_artifact_cache(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """The same bounded EventCard input reuses a validated score batch across TaskRuns."""
    event = create_event(migrated_session_factory, key="cache")
    first_run_id, first_step_id = create_task_provenance(migrated_session_factory)
    second_run_id, second_step_id = create_task_provenance(migrated_session_factory)
    provider = FakeLLMProvider({"scores": [event_score(event.id, importance=80, relevance=70)]})
    service = AIEditorialService(migrated_session_factory, provider)

    asyncio.run(
        service.score_events(
            (event.id,),
            task_run_id=first_run_id,
            task_step_id=first_step_id,
            budget=BudgetController(),
        )
    )
    reused = asyncio.run(
        service.score_events(
            (event.id,),
            task_run_id=second_run_id,
            task_step_id=second_step_id,
            budget=BudgetController(),
        )
    )

    assert reused.cache_hit is True
    assert provider.calls == 1
    assert artifact_count(migrated_session_factory) == 1


def test_ranking_persists_scores_and_selects_the_top_configured_count(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """Code sorts LLM scores deterministically and applies the configurable top-N cap."""
    low = create_event(migrated_session_factory, key="low", deterministic_score=90)
    high = create_event(migrated_session_factory, key="high", deterministic_score=10)
    middle = create_event(migrated_session_factory, key="middle", deterministic_score=50)
    task_run_id, task_step_id = create_task_provenance(migrated_session_factory)
    provider = FakeLLMProvider(
        {
            "scores": [
                event_score(low.id, importance=55, relevance=80),
                event_score(high.id, importance=95, relevance=60),
                event_score(middle.id, importance=80, relevance=90),
            ]
        }
    )
    service = AIEditorialService(
        migrated_session_factory,
        provider,
        max_candidates=30,
        max_selected_events=2,
    )

    result = asyncio.run(
        service.score_events(
            (low.id, high.id, middle.id),
            task_run_id=task_run_id,
            task_step_id=task_step_id,
            budget=BudgetController(),
        )
    )

    assert result.selected_event_ids == (high.id, middle.id)
    with UnitOfWork(migrated_session_factory) as unit:
        assert unit.session is not None
        events = {event.id: event for event in unit.session.scalars(select(NewsEvent))}
        assert events[high.id].status is NewsEventStatus.SELECTED
        assert events[middle.id].status is NewsEventStatus.SELECTED
        assert events[low.id].status is NewsEventStatus.REJECTED
        assert events[high.id].importance_score == 95
        assert events[high.id].relevance_score == 60
        assert events[high.id].confidence_score == 80
        assert events[high.id].selection_reason == f"Reason for event {high.id}"
        assert events[high.id].risk_flags_json == '["verify wording"]'


def test_ranking_reserves_a_domestic_story_and_caps_ai_stories(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """A stronger AI score cannot crowd every domestic story out of an episode."""
    candidates = (
        create_event(
            migrated_session_factory,
            key="hn-ai",
            source_id="hacker-news-rss",
            title="Claude model update changes enterprise coding workflows",
        ),
        create_event(
            migrated_session_factory,
            key="ithome-ai",
            source_id="ithome-rss",
            title="DeepSeek 发布新一代大模型",
        ),
        create_event(
            migrated_session_factory,
            key="oschina-ai",
            source_id="oschina-news-rss",
            title="OpenAI 发布新的 GPT 模型",
        ),
        create_event(
            migrated_session_factory,
            key="sspai-tech",
            source_id="sspai-rss",
            title="Gemini AI 新功能进入手机系统",
        ),
        create_event(
            migrated_session_factory,
            key="domestic",
            source_id="chinanews-china-rss",
            title="国务院部署促进消费和稳定就业的重点工作",
        ),
    )
    task_run_id, task_step_id = create_task_provenance(migrated_session_factory)
    provider = FakeLLMProvider(
        {
            "scores": [
                event_score(candidates[0].id, importance=100, relevance=100),
                event_score(candidates[1].id, importance=95, relevance=95),
                event_score(candidates[2].id, importance=90, relevance=90),
                event_score(candidates[3].id, importance=85, relevance=85),
                event_score(candidates[4].id, importance=80, relevance=80),
            ]
        }
    )
    service = AIEditorialService(
        migrated_session_factory,
        provider,
        max_selected_events=4,
    )

    result = asyncio.run(
        service.score_events(
            tuple(candidate.id for candidate in candidates),
            task_run_id=task_run_id,
            task_step_id=task_step_id,
            budget=BudgetController(),
        )
    )

    assert result.selected_event_ids == (
        candidates[0].id,
        candidates[1].id,
        candidates[2].id,
        candidates[4].id,
    )


def test_ranking_caps_ai_stories_when_other_topics_are_available(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """The AI cap leaves room for a lower-ranked non-AI story on a tech-heavy day."""
    candidates = (
        create_event(
            migrated_session_factory,
            key="hn-ai-only",
            source_id="hacker-news-rss",
            title="Claude model update changes enterprise coding workflows",
        ),
        create_event(
            migrated_session_factory,
            key="ithome-ai-only",
            source_id="ithome-rss",
            title="DeepSeek 发布新一代大模型",
        ),
        create_event(
            migrated_session_factory,
            key="oschina-ai-only",
            source_id="oschina-news-rss",
            title="OpenAI 发布新的 GPT 模型",
        ),
        create_event(
            migrated_session_factory,
            key="sspai-ai-only",
            source_id="sspai-rss",
            title="Gemini AI 新功能进入手机系统",
        ),
        create_event(
            migrated_session_factory,
            key="non-ai",
            source_id="source-non-ai",
            title="手机系统更新带来新的隐私设置",
        ),
    )
    task_run_id, task_step_id = create_task_provenance(migrated_session_factory)
    provider = FakeLLMProvider(
        {
            "scores": [
                event_score(candidates[0].id, importance=100, relevance=100),
                event_score(candidates[1].id, importance=95, relevance=95),
                event_score(candidates[2].id, importance=90, relevance=90),
                event_score(candidates[3].id, importance=85, relevance=85),
                event_score(candidates[4].id, importance=80, relevance=80),
            ]
        }
    )
    service = AIEditorialService(
        migrated_session_factory,
        provider,
        max_selected_events=4,
    )

    result = asyncio.run(
        service.score_events(
            tuple(candidate.id for candidate in candidates),
            task_run_id=task_run_id,
            task_step_id=task_step_id,
            budget=BudgetController(),
        )
    )

    assert result.selected_event_ids == (
        candidates[0].id,
        candidates[1].id,
        candidates[2].id,
        candidates[4].id,
    )


def test_score_for_unknown_event_id_is_rejected_before_cache_persistence(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """The LLM cannot introduce an event outside the EventCard identifier allowlist."""
    event = create_event(migrated_session_factory, key="unknown-id")
    task_run_id, task_step_id = create_task_provenance(migrated_session_factory)
    service = AIEditorialService(
        migrated_session_factory,
        FakeLLMProvider({"scores": [event_score(999_999, importance=90, relevance=90)]}),
    )

    with pytest.raises(LLMResponseValidationError):
        asyncio.run(
            service.score_events(
                (event.id,),
                task_run_id=task_run_id,
                task_step_id=task_step_id,
                budget=BudgetController(),
            )
        )

    assert artifact_count(migrated_session_factory) == 0


def test_ranking_budget_is_checked_before_calling_the_provider(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """Budget exhaustion stops a ranking miss before the fake provider receives a request."""
    event = create_event(migrated_session_factory, key="budget")
    task_run_id, task_step_id = create_task_provenance(migrated_session_factory)
    provider = FakeLLMProvider({"scores": [event_score(event.id, importance=90, relevance=90)]})
    service = AIEditorialService(migrated_session_factory, provider)

    with pytest.raises(AIBudgetExceededError):
        asyncio.run(
            service.score_events(
                (event.id,),
                task_run_id=task_run_id,
                task_step_id=task_step_id,
                budget=BudgetController(max_calls=0, max_input_tokens=100, max_output_tokens=100),
            )
        )

    assert provider.calls == 0


def test_editorial_configuration_has_documented_candidate_and_selection_defaults(
    app_config_path: Path,
) -> None:
    """Editorial limits are explicit settings rather than hidden ranking-service constants."""
    settings = load_settings(config_path=app_config_path)

    assert settings.editorial.max_candidates == 30
    assert settings.editorial.max_selected_events == 8
    assert settings.editorial.max_ai_events == 3
    assert settings.editorial.min_domestic_events_when_available == 2


def test_ranking_step_consumes_clustered_event_ids_and_records_selection(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """The ranking checkpoint consumes clustering output and exposes persisted selected IDs."""
    event = create_event(migrated_session_factory, key="step")
    task_run_id, task_step_id = create_task_provenance(migrated_session_factory)
    service = AIEditorialService(
        migrated_session_factory,
        FakeLLMProvider({"scores": [event_score(event.id, importance=90, relevance=90)]}),
    )
    step = RankingStep(service, budget_factory=BudgetController)
    context = PipelineContext(
        task_run_id=task_run_id,
        session_factory=migrated_session_factory,
        shutdown_requested=asyncio.Event(),
        clock=Clock(),
        values={
            "news_event_ids": (event.id,),
            "active_task_step_id": task_step_id,
        },
    )

    result = asyncio.run(step.run(context))

    assert result.input_count == 1
    assert result.output_count == 1
    assert context.values["selected_news_event_ids"] == (event.id,)
    assert result.details["cache_hit"] is False
