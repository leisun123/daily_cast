"""Metadata generation stays bounded, strict, and artifact-cached."""

from __future__ import annotations

import asyncio
from pathlib import Path

from editorial_test_support import (
    FakeLLMProvider,
    artifact_count,
    build_dossiers,
    build_outline,
    canonical_messages,
    create_selected_event,
    create_task_provenance,
    upgraded_session_factory,
    valid_script_payload,
)
from sqlalchemy.orm import Session, sessionmaker

from dailycast.db.models import LLMOperation
from dailycast.llm.budget import BudgetController
from dailycast.llm.editorial_service import AIEditorialService
from dailycast.llm.script_schemas import EpisodeScript


def test_metadata_is_cached_and_never_receives_full_evidence(
    app_config_path: Path,
) -> None:
    """Metadata only uses selected titles and the final bounded script projection."""
    factory: sessionmaker[Session] = upgraded_session_factory(app_config_path)
    try:
        full_article_sentinel = "FULL_ARTICLE_BODY_MUST_NOT_ENTER_METADATA_PROMPT"
        fixture = create_selected_event(
            factory,
            key="metadata-cache",
            content=full_article_sentinel,
        )
        outline = build_outline(fixture.event_id)
        dossiers = build_dossiers(factory, fixture)
        script = EpisodeScript.model_validate(
            valid_script_payload(outline, fixture),
            context={"outline": outline, "evidence_dossiers": dossiers},
        )
        provider = FakeLLMProvider(
            {
                LLMOperation.GENERATE_METADATA: [
                    {
                        "schema_version": "1",
                        "title": "今日科技新闻",
                        "description": "围绕一项经过核验的科技新闻展开。",
                        "keywords": ["科技", "新闻"],
                    }
                ]
            }
        )
        task_run_id, task_step_id = create_task_provenance(
            factory, step_name="checking", step_order=9
        )
        service = AIEditorialService(factory, provider)
        first = asyncio.run(
            service.generate_metadata(
                script,
                ["事件 metadata-cache"],
                estimated_duration_seconds=120,
                task_run_id=task_run_id,
                task_step_id=task_step_id,
                budget=BudgetController(),
            )
        )
        second = asyncio.run(
            service.generate_metadata(
                script,
                ["事件 metadata-cache"],
                estimated_duration_seconds=120,
                task_run_id=task_run_id,
                task_step_id=task_step_id,
                budget=BudgetController(),
            )
        )

        assert first.metadata.title == "今日科技新闻"
        assert not first.cache_hit
        assert second.cache_hit
        assert provider.calls_by_operation[LLMOperation.GENERATE_METADATA] == 1
        assert artifact_count(factory) == 1
        assert full_article_sentinel not in canonical_messages(provider)
    finally:
        factory.kw["bind"].dispose()
