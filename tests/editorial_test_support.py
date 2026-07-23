"""Shared deterministic SQLite fixtures and fake provider for editorial workflow tests."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from alembic import command
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from dailycast.core.config import load_settings
from dailycast.core.hashes import sha256_text
from dailycast.db.models import (
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
from dailycast.llm.contracts import LLMMessage, LLMUsage, StructuredResult
from dailycast.llm.editorial_service import AIEditorialService
from dailycast.llm.outline_schemas import EpisodeOutline, EvidenceDossier


@dataclass(frozen=True, slots=True)
class EditorialFixture:
    """Durable source, Article, and selected-event IDs for one test evidence package."""

    event_id: int
    article_id: int
    source_id: str


class FakeLLMProvider:
    """In-memory operation responses that record bounded requests and never use a network."""

    provider_name = "fake"
    model = "fake-editorial-model"
    max_output_tokens = 100

    def __init__(self, responses: dict[LLMOperation, Sequence[dict[str, object]]]) -> None:
        self._responses = {operation: list(values) for operation, values in responses.items()}
        self.calls = 0
        self.calls_by_operation: dict[LLMOperation, int] = {}
        self.messages: list[tuple[LLMOperation, tuple[LLMMessage, ...]]] = []

    def generation_config_hash(self, model_options: dict[str, object]) -> str:
        """Return a stable non-secret configuration identity used by artifact-cache tests."""
        del model_options
        return sha256_text("fake-script-config-v1")

    async def generate_structured(
        self,
        operation: LLMOperation,
        messages: tuple[LLMMessage, ...],
        response_schema: type[BaseModel],
        model_options: dict[str, object],
    ) -> StructuredResult:
        """Return the next configured structured payload without a network call."""
        del response_schema, model_options
        values = self._responses.get(operation)
        if not values:
            raise AssertionError(f"unexpected fake-provider operation: {operation}")
        self.calls += 1
        self.calls_by_operation[operation] = self.calls_by_operation.get(operation, 0) + 1
        self.messages.append((operation, messages))
        content = values.pop(0)
        return StructuredResult(
            content=content,
            model=self.model,
            usage=LLMUsage(input_tokens=5, output_tokens=7),
            request_id=f"fake-editorial-{self.calls}",
        )


def upgraded_session_factory(app_config_path: Path) -> sessionmaker[Session]:
    """Create one empty SQLite test database by running the actual Alembic upgrade path."""
    settings = load_settings(config_path=app_config_path)
    command.upgrade(
        build_alembic_config(
            ini_path=Path(__file__).resolve().parents[1] / "alembic.ini",
            database_url=settings.database.url,
        ),
        "head",
    )
    return create_session_factory(create_sqlite_engine(settings.database))


def create_selected_event(
    factory: sessionmaker[Session], *, key: str, content: str
) -> EditorialFixture:
    """Persist one ranked selected event with a traceable extracted Article."""
    now = datetime(2026, 7, 22, 12, tzinfo=UTC)
    source_id = f"script-source-{key}"
    with UnitOfWork(factory) as unit:
        assert unit.session is not None
        SourceRepository(unit.session).create(
            id=source_id,
            name=f"Source {key}",
            kind=SourceKind.RSS,
            entry_url=f"https://{source_id}.example.test/rss",
            normalized_entry_url=f"https://{source_id}.example.test/rss",
            priority=80,
            config_json="{}",
        )
        url = f"https://news.example.test/{key}"
        article = ArticleRepository(unit.session).upsert(
            source_id=source_id,
            external_id=key,
            url=url,
            normalized_url=url,
            url_hash=sha256_text(url),
            title=f"事件 {key}",
            normalized_title=f"事件 {key}",
            title_hash=sha256_text(f"事件 {key}"),
            summary="这是经过验证的简短新闻摘要。",
            content_text=content,
            content_hash=sha256_text(content),
            language="zh",
            published_at=now,
            discovered_at=now,
            status=ArticleStatus.ELIGIBLE,
            metadata_json="{}",
        )
        event = NewsEventRepository(unit.session).create(
            event_key=f"2026-07-22:script:{key}",
            event_date=now.date(),
            representative_article_id=article.id,
            title=f"事件 {key}",
            summary="这是经过验证的简短新闻摘要。",
            status=NewsEventStatus.SELECTED,
            first_published_at=now,
            last_published_at=now,
            article_count=1,
            source_count=1,
            deterministic_score=90,
            importance_score=90,
            relevance_score=80,
            confidence_score=70,
            selection_reason="重要且相关。",
            risk_flags_json="[]",
            cluster_algorithm="tfidf_char",
            cluster_version="1",
            cluster_threshold=0.58,
            cluster_signature=sha256_text(key),
        )
        ArticleRepository(unit.session).update(article, news_event_id=event.id)
        return EditorialFixture(event.id, article.id, source_id)


def create_task_provenance(
    factory: sessionmaker[Session], *, step_name: str = "scripting", step_order: int = 8
) -> tuple[str, int]:
    """Create one running TaskRun and active TaskStep for LLMArtifact provenance tests."""
    task_run_id = str(uuid4())
    with UnitOfWork(factory) as unit:
        assert unit.session is not None
        task_run = TaskRunRepository(unit.session).create(
            id=task_run_id,
            task_type=TaskType.DAILY_GENERATE,
            business_key=f"script-test:{task_run_id}",
            idempotency_key=f"script-test:{task_run_id}",
            trigger_type=TriggerType.MANUAL,
            status=TaskRunStatus.RUNNING,
            pipeline_version="script-test-v1",
            config_fingerprint="a" * 64,
            config_snapshot_json="{}",
            request_json="{}",
        )
        task_step = TaskStepRepository(unit.session).create(
            task_run_id=task_run.id,
            step_name=step_name,
            step_order=step_order,
            attempt=1,
            status="running",
            details_json="{}",
        )
        return task_run.id, task_step.id


def build_outline(event_id: int) -> EpisodeOutline:
    """Return a small complete validated outline with one selected news event."""
    return EpisodeOutline.model_validate(
        {
            "schema_version": "1",
            "title_angle": "今日科技新闻",
            "target_seconds": 120,
            "sections": [
                {
                    "section_id": "intro",
                    "type": "intro",
                    "event_ids": [],
                    "goal": "开场。",
                    "key_facts": [],
                    "seconds": 15,
                },
                {
                    "section_id": "news-1",
                    "type": "news",
                    "event_ids": [event_id],
                    "goal": "讲解事件。",
                    "key_facts": ["使用证据。"],
                    "seconds": 90,
                },
                {
                    "section_id": "outro",
                    "type": "outro",
                    "event_ids": [],
                    "goal": "结尾。",
                    "key_facts": [],
                    "seconds": 15,
                },
            ],
        },
        context={
            "selected_event_ids": (event_id,),
            "target_duration_seconds": 120,
            "duration_tolerance_seconds": 0,
            "max_outline_sections": 12,
        },
    )


def build_dossiers(
    factory: sessionmaker[Session], fixture: EditorialFixture
) -> tuple[EvidenceDossier, ...]:
    """Build actual bounded evidence through the existing Sprint 4B-2 implementation."""
    service = AIEditorialService(factory, FakeLLMProvider({}))
    return service.build_evidence_dossiers((fixture.event_id,)).dossiers


def valid_script_payload(
    outline: EpisodeOutline, fixture: EditorialFixture, *, text: str | None = None
) -> dict[str, object]:
    """Build a traceable JSON script response with stable outline order and article provenance."""
    spoken = text or "这是一段适合中文播报的简洁新闻内容。" * 28
    return {
        "schema_version": "1",
        "sections": [
            {
                "section_id": "intro",
                "text": "欢迎收听今天的 DailyCast。",
                "event_ids": [],
                "article_ids": [],
                "claims": [],
            },
            {
                "section_id": "news-1",
                "text": spoken,
                "event_ids": [fixture.event_id],
                "article_ids": [fixture.article_id],
                "claims": [{"text": "事件正在持续发展。", "article_ids": [fixture.article_id]}],
            },
            {
                "section_id": "outro",
                "text": "以上就是今天的节目，感谢收听。",
                "event_ids": [],
                "article_ids": [],
                "claims": [],
            },
        ],
        "pronunciation_hints": [{"term": "OpenAI", "pronunciation": "Open A I"}],
    }


def artifact_count(factory: sessionmaker[Session]) -> int:
    """Return the count of successful schema-validated structured LLM artifacts."""
    with UnitOfWork(factory) as unit:
        assert unit.session is not None
        count = unit.session.scalar(select(func.count()).select_from(LLMArtifact))
        assert count is not None
        return count


def canonical_messages(provider: FakeLLMProvider) -> str:
    """Return the exact bounded request content captured by the fake provider for assertions."""
    return "\n".join(message.content for _, messages in provider.messages for message in messages)


def json_file(path: Path) -> dict[str, object]:
    """Read one canonical structured artifact for checkpoint integration assertions."""
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded
