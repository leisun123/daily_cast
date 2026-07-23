"""Sprint 4B-2 bounded evidence dossiers and outline-generation workflow tests."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from dailycast.core.config import load_settings
from dailycast.core.hashes import sha256_text
from dailycast.core.time import Clock
from dailycast.db.models import (
    Article,
    ArticleStatus,
    LLMArtifact,
    LLMOperation,
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
from dailycast.llm.prompts import PromptTemplate
from dailycast.pipeline.context import PipelineContext
from dailycast.pipeline.steps.outlining import OutliningStep


class FakeOutlineProvider:
    """Return operation-specific structured output while retaining only bounded requests."""

    provider_name = "fake"
    model = "fake-outline-model"
    max_output_tokens = 100

    def __init__(self, responses: dict[LLMOperation, dict[str, object]]) -> None:
        self._responses = responses
        self.calls = 0
        self.messages: list[tuple[LLMMessage, ...]] = []

    def generation_config_hash(self, model_options: dict[str, object]) -> str:
        """Provide a stable non-secret identity for artifact-cache tests."""
        del model_options
        return sha256_text("fake-outline-config-v1")

    async def generate_structured(
        self,
        operation: LLMOperation,
        messages: tuple[LLMMessage, ...],
        response_schema: type[BaseModel],
        model_options: dict[str, object],
    ) -> StructuredResult:
        """Return the configured payload without contacting a model provider."""
        del response_schema, model_options
        self.calls += 1
        self.messages.append(messages)
        return StructuredResult(
            content=self._responses[operation],
            model=self.model,
            usage=LLMUsage(input_tokens=5, output_tokens=7),
            request_id=f"fake-outline-{self.calls}",
        )


@dataclass(frozen=True, slots=True)
class EventFixture:
    """Stable identifiers for one persisted NewsEvent and its source Articles."""

    event_id: int
    article_ids: tuple[int, ...]


@pytest.fixture
def migrated_session_factory(app_config_path: Path) -> sessionmaker[Session]:
    """Create an isolated database through the real Alembic upgrade path."""
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


def create_selected_event(
    factory: sessionmaker[Session],
    *,
    key: str,
    articles: tuple[tuple[str, int, str, str | None, str | None], ...],
    importance_score: float = 90,
    relevance_score: float = 80,
    confidence_score: float = 70,
) -> EventFixture:
    """Persist one selected event with deterministic, independently configurable evidence."""
    now = datetime(2026, 7, 22, 12, tzinfo=UTC)
    with UnitOfWork(factory) as unit:
        assert unit.session is not None
        article_repository = ArticleRepository(unit.session)
        persisted_articles: list[Article] = []
        for index, (source_suffix, priority, title, summary, content) in enumerate(
            articles, start=1
        ):
            source_id = f"source-{key}-{source_suffix}"
            if SourceRepository(unit.session).get(source_id) is None:
                SourceRepository(unit.session).create(
                    id=source_id,
                    name=f"Source {source_suffix}",
                    kind=SourceKind.RSS,
                    entry_url=f"https://{source_id}.example.test/rss",
                    normalized_entry_url=f"https://{source_id}.example.test/rss",
                    priority=priority,
                    config_json="{}",
                )
            url = f"https://news.example.test/{key}/{index}"
            persisted_articles.append(
                article_repository.upsert(
                    source_id=source_id,
                    external_id=f"{key}-{index}",
                    url=url,
                    normalized_url=url,
                    url_hash=sha256_text(url),
                    title=title,
                    normalized_title=title.lower(),
                    title_hash=sha256_text(title.lower()),
                    summary=summary,
                    content_text=content,
                    content_hash=sha256_text(content) if content is not None else None,
                    language="en",
                    published_at=now - timedelta(minutes=index),
                    discovered_at=now,
                    status=ArticleStatus.ELIGIBLE,
                    metadata_json="{}",
                )
            )
        representative = persisted_articles[0]
        event = NewsEventRepository(unit.session).create(
            event_key=f"2026-07-22:{key}",
            event_date=now.date(),
            representative_article_id=representative.id,
            title=f"Event {key}",
            summary=f"Bounded summary for {key}.",
            status=NewsEventStatus.SELECTED,
            first_published_at=now - timedelta(hours=1),
            last_published_at=now,
            article_count=len(persisted_articles),
            source_count=len({article.source_id for article in persisted_articles}),
            deterministic_score=50.0,
            importance_score=importance_score,
            relevance_score=relevance_score,
            confidence_score=confidence_score,
            selection_reason=f"Selected because {key} matters.",
            risk_flags_json="[]",
            cluster_algorithm="tfidf_char",
            cluster_version="1",
            cluster_threshold=0.58,
            cluster_signature=sha256_text(key),
        )
        for article in persisted_articles:
            article_repository.update(article, news_event_id=event.id)
        return EventFixture(event.id, tuple(article.id for article in persisted_articles))


def create_task_provenance(
    factory: sessionmaker[Session], *, step_name: str = "outlining"
) -> tuple[str, int]:
    """Create an active TaskRun and TaskStep used as LLMArtifact provenance."""
    task_run_id = str(uuid4())
    with UnitOfWork(factory) as unit:
        assert unit.session is not None
        task_run = TaskRunRepository(unit.session).create(
            id=task_run_id,
            task_type=TaskType.DAILY_GENERATE,
            business_key=f"outline-test:{task_run_id}",
            idempotency_key=f"outline-test:{task_run_id}",
            trigger_type=TriggerType.MANUAL,
            status=TaskRunStatus.RUNNING,
            pipeline_version="outline-test-v1",
            config_fingerprint="a" * 64,
            config_snapshot_json="{}",
            request_json="{}",
        )
        task_step = TaskStepRepository(unit.session).create(
            task_run_id=task_run.id,
            step_name=step_name,
            step_order=7,
            attempt=1,
            status="running",
            details_json="{}",
        )
        return task_run.id, task_step.id


def artifact_count(factory: sessionmaker[Session]) -> int:
    """Return the number of schema-validated, reusable LLM artifacts."""
    with UnitOfWork(factory) as unit:
        assert unit.session is not None
        count = unit.session.scalar(select(func.count()).select_from(LLMArtifact))
        assert count is not None
        return count


def valid_outline(event_ids: tuple[int, ...], *, target_seconds: int = 900) -> dict[str, object]:
    """Build a valid outline whose news sections cover every selected event exactly once."""
    intro_seconds = 30
    outro_seconds = 30
    available_news_seconds = target_seconds - intro_seconds - outro_seconds
    per_event, remainder = divmod(available_news_seconds, len(event_ids))
    sections: list[dict[str, object]] = [
        {
            "section_id": "intro",
            "type": "intro",
            "event_ids": [],
            "goal": "Frame the daily briefing.",
            "key_facts": [],
            "seconds": intro_seconds,
        }
    ]
    for index, event_id in enumerate(event_ids, start=1):
        seconds = per_event + (1 if index <= remainder else 0)
        sections.append(
            {
                "section_id": f"news-{index}",
                "type": "news",
                "event_ids": [event_id],
                "goal": f"Explain event {event_id} from the supplied evidence.",
                "key_facts": [f"Use the verified evidence for event {event_id}."],
                "seconds": seconds,
            }
        )
    sections.append(
        {
            "section_id": "outro",
            "type": "outro",
            "event_ids": [],
            "goal": "Close the briefing.",
            "key_facts": [],
            "seconds": outro_seconds,
        }
    )
    return {
        "schema_version": "1",
        "title_angle": "A concise evidence-first daily briefing",
        "target_seconds": target_seconds,
        "sections": sections,
    }


def outline_service(
    factory: sessionmaker[Session],
    response: dict[str, object],
    **settings: object,
) -> tuple[AIEditorialService, FakeOutlineProvider]:
    """Create the service with only the explicit outline limits under test."""
    provider = FakeOutlineProvider({LLMOperation.GENERATE_OUTLINE: response})
    options: dict[str, object] = {
        "max_sources_per_event": 3,
        "max_chars_per_source": 1200,
        "max_total_evidence_chars": 24_000,
        "target_duration_seconds": 900,
        "max_outline_sections": 12,
        "min_publishable_events": 1,
    }
    options.update(settings)
    return AIEditorialService(factory, provider, **options), provider


def test_evidence_dossier_places_representative_first_and_prefers_distinct_sources(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """The fixed representative is first; remaining slots prefer distinct sources by rank."""
    event = create_selected_event(
        migrated_session_factory,
        key="distinct",
        articles=(
            ("representative", 10, "Representative", "Rep summary", "Rep evidence"),
            ("high", 90, "High", "High summary", "High evidence"),
            ("middle", 50, "Middle", "Middle summary", "Middle evidence"),
            ("representative", 10, "Repeat", "Repeat summary", "Repeated source evidence"),
        ),
    )
    service, _ = outline_service(migrated_session_factory, valid_outline((event.event_id,)))

    built = service.build_evidence_dossiers((event.event_id,))
    dossier = built.dossiers[0]

    assert dossier.representative_article.article_id == event.article_ids[0]
    assert [source.article_id for source in dossier.evidence_sources] == list(event.article_ids[:3])
    assert [source.source_id for source in dossier.evidence_sources] == [
        "source-distinct-representative",
        "source-distinct-high",
        "source-distinct-middle",
    ]


def test_evidence_dossier_enforces_per_source_and_total_character_limits(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """No evidence package exceeds either source-level or total configured character budgets."""
    first = create_selected_event(
        migrated_session_factory,
        key="limit-one",
        articles=(("a", 50, "A", None, "A" * 100),),
    )
    second = create_selected_event(
        migrated_session_factory,
        key="limit-two",
        articles=(("b", 50, "B", None, "B" * 100),),
    )
    service, _ = outline_service(
        migrated_session_factory,
        valid_outline((first.event_id, second.event_id)),
        max_sources_per_event=2,
        max_chars_per_source=12,
        max_total_evidence_chars=20,
    )

    built = service.build_evidence_dossiers((first.event_id, second.event_id))

    assert all(
        len(source.text_excerpt) <= 12
        for dossier in built.dossiers
        for source in dossier.evidence_sources
    )
    assert built.total_evidence_chars <= 20


def test_outline_request_never_contains_the_complete_article_content(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """The outline model receives only bounded normalized evidence, never raw full article text."""
    full_content = "FULL_ARTICLE_SENTINEL " * 500
    event = create_selected_event(
        migrated_session_factory,
        key="bounded-request",
        articles=(("source", 50, "Bounded", None, full_content),),
    )
    task_run_id, task_step_id = create_task_provenance(migrated_session_factory)
    service, provider = outline_service(
        migrated_session_factory,
        valid_outline((event.event_id,)),
        max_chars_per_source=120,
    )
    dossiers = service.build_evidence_dossiers((event.event_id,)).dossiers

    asyncio.run(
        service.generate_outline(
            (event.event_id,),
            dossiers,
            task_run_id=task_run_id,
            task_step_id=task_step_id,
            budget=BudgetController(),
        )
    )

    sent = "\n".join(message.content for message in provider.messages[0])
    assert full_content not in sent
    assert len(dossiers[0].evidence_sources[0].text_excerpt) == 120


def test_generate_outline_returns_a_schema_valid_outline(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """A valid model response is validated, cached, and returned as a structured outline."""
    event = create_selected_event(
        migrated_session_factory,
        key="valid-outline",
        articles=(("source", 50, "Article", "Summary", "Evidence"),),
    )
    task_run_id, task_step_id = create_task_provenance(migrated_session_factory)
    service, _ = outline_service(migrated_session_factory, valid_outline((event.event_id,)))
    dossiers = service.build_evidence_dossiers((event.event_id,)).dossiers

    result = asyncio.run(
        service.generate_outline(
            (event.event_id,),
            dossiers,
            task_run_id=task_run_id,
            task_step_id=task_step_id,
            budget=BudgetController(),
        )
    )

    assert result.outline.target_seconds == 900
    assert result.outline.sections[1].event_ids == (event.event_id,)
    assert result.cache_hit is False
    assert artifact_count(migrated_session_factory) == 1


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (
            lambda outline, event_id: outline["sections"].__setitem__(
                1, {**outline["sections"][1], "event_ids": [event_id + 999]}
            ),
            "unknown event ID",
        ),
        (
            lambda outline, event_id: outline["sections"].__setitem__(
                1, {**outline["sections"][1], "event_ids": []}
            ),
            "missing selected event",
        ),
        (
            lambda outline, event_id: outline["sections"].__setitem__(
                2, {**outline["sections"][2], "section_id": "news-1"}
            ),
            "duplicate section ID",
        ),
        (
            lambda outline, event_id: outline.__setitem__("target_seconds", 300),
            "duration mismatch",
        ),
    ],
)
def test_invalid_outline_semantics_are_rejected_without_cache_persistence(
    migrated_session_factory: sessionmaker[Session],
    mutator: object,
    message: str,
) -> None:
    """Unknown, omitted, duplicate, or duration-invalid output cannot become a reusable artifact."""
    event = create_selected_event(
        migrated_session_factory,
        key=f"invalid-{message}",
        articles=(("source", 50, "Article", "Summary", "Evidence"),),
    )
    response = valid_outline((event.event_id,))
    assert callable(mutator)
    mutator(response, event.event_id)
    task_run_id, task_step_id = create_task_provenance(migrated_session_factory)
    service, _ = outline_service(migrated_session_factory, response)
    dossiers = service.build_evidence_dossiers((event.event_id,)).dossiers

    with pytest.raises(LLMResponseValidationError):
        asyncio.run(
            service.generate_outline(
                (event.event_id,),
                dossiers,
                task_run_id=task_run_id,
                task_step_id=task_step_id,
                budget=BudgetController(),
            )
        )

    assert artifact_count(migrated_session_factory) == 0


def test_generate_outline_reuses_exact_cache_and_prompt_version_changes_miss(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """Validated outlines reuse only an exact prompt-version identity across TaskRuns."""
    event = create_selected_event(
        migrated_session_factory,
        key="outline-cache",
        articles=(("source", 50, "Article", "Summary", "Evidence"),),
    )
    first_run_id, first_step_id = create_task_provenance(migrated_session_factory)
    second_run_id, second_step_id = create_task_provenance(migrated_session_factory)
    service, provider = outline_service(migrated_session_factory, valid_outline((event.event_id,)))
    dossiers = service.build_evidence_dossiers((event.event_id,)).dossiers

    asyncio.run(
        service.generate_outline(
            (event.event_id,),
            dossiers,
            task_run_id=first_run_id,
            task_step_id=first_step_id,
            budget=BudgetController(),
        )
    )
    cached = asyncio.run(
        service.generate_outline(
            (event.event_id,),
            dossiers,
            task_run_id=second_run_id,
            task_step_id=second_step_id,
            budget=BudgetController(),
        )
    )
    changed_prompt_service = AIEditorialService(
        migrated_session_factory,
        provider,
        outline_prompt=PromptTemplate(
            version="generate_outline_v2",
            system_instruction="Return the same strict JSON contract.",
        ),
    )
    changed_dossiers = changed_prompt_service.build_evidence_dossiers((event.event_id,)).dossiers
    changed = asyncio.run(
        changed_prompt_service.generate_outline(
            (event.event_id,),
            changed_dossiers,
            task_run_id=second_run_id,
            task_step_id=second_step_id,
            budget=BudgetController(),
        )
    )

    assert cached.cache_hit is True
    assert changed.cache_hit is False
    assert provider.calls == 2
    assert artifact_count(migrated_session_factory) == 2


def test_outlining_step_writes_a_schema_valid_atomic_artifact_and_counts(
    migrated_session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """The outlining checkpoint stores canonical validated output below the controlled data root."""
    event = create_selected_event(
        migrated_session_factory,
        key="checkpoint",
        articles=(("source", 50, "Article", "Summary", "Evidence"),),
    )
    task_run_id, task_step_id = create_task_provenance(migrated_session_factory)
    service, _ = outline_service(migrated_session_factory, valid_outline((event.event_id,)))
    step = OutliningStep(service, data_dir=tmp_path)
    context = PipelineContext(
        task_run_id=task_run_id,
        session_factory=migrated_session_factory,
        shutdown_requested=asyncio.Event(),
        clock=Clock(),
        values={
            "selected_news_event_ids": (event.event_id,),
            "active_task_step_id": task_step_id,
        },
    )

    result = asyncio.run(step.run(context))

    assert result.input_count == 1
    assert result.output_count == 3
    assert result.artifact_path == f"work/{task_run_id}/editorial/outline.json"
    assert result.details["artifact_path"] == result.artifact_path
    assert result.details["source_article_count"] == 1
    assert result.details["total_evidence_chars"] == len("Evidence")
    artifact = tmp_path / result.artifact_path
    assert json.loads(artifact.read_text(encoding="utf-8"))["schema_version"] == "1"
    assert not artifact.with_suffix(".json.tmp").exists()


def test_partial_evidence_is_bounded_and_deterministic(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """Missing body text falls back deterministically without dropping the selected event."""
    event = create_selected_event(
        migrated_session_factory,
        key="partial",
        articles=(
            ("representative", 20, "Representative fallback", None, None),
            ("secondary", 90, "Secondary", None, "Secondary evidence"),
        ),
    )
    service, _ = outline_service(migrated_session_factory, valid_outline((event.event_id,)))

    first = service.build_evidence_dossiers((event.event_id,))
    second = service.build_evidence_dossiers((event.event_id,))

    assert first == second
    assert first.dossiers[0].evidence_sources[0].article_id == event.article_ids[0]
    assert first.dossiers[0].evidence_sources[0].text_excerpt == "Representative fallback"
    assert first.source_article_count == 2


def test_editorial_configuration_has_explicit_evidence_and_outline_defaults(
    app_config_path: Path,
) -> None:
    """Evidence and outline limits live in configuration rather than hidden service constants."""
    settings = load_settings(config_path=app_config_path)

    assert settings.editorial.max_sources_per_event == 3
    assert settings.editorial.max_chars_per_source == 1200
    assert settings.editorial.max_total_evidence_chars == 24_000
    assert settings.editorial.target_duration_seconds == 900
    assert settings.editorial.max_outline_sections == 12
