"""Metadata factual detail is limited to the bounded final-script input."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from editorial_test_support import (
    FakeLLMProvider,
    artifact_count,
    build_dossiers,
    build_outline,
    create_selected_event,
    create_task_provenance,
    upgraded_session_factory,
    valid_script_payload,
)
from sqlalchemy.orm import Session, sessionmaker

from dailycast.core.errors import DailyCastError
from dailycast.db.models import LLMOperation
from dailycast.llm.budget import BudgetController
from dailycast.llm.editorial_service import AIEditorialService
from dailycast.llm.script_schemas import EpisodeScript


def test_metadata_numeric_claim_absent_from_bounded_input_is_not_cached(
    app_config_path: Path,
) -> None:
    """A novel number in generated metadata fails local provenance validation before caching."""
    factory: sessionmaker[Session] = upgraded_session_factory(app_config_path)
    try:
        fixture = create_selected_event(factory, key="metadata-provenance", content="可信证据。")
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
                        "title": "9999 条新闻",
                        "description": "围绕一项经过核验的科技新闻展开。",
                        "keywords": ["科技"],
                    }
                ]
            }
        )
        task_run_id, task_step_id = create_task_provenance(
            factory, step_name="checking", step_order=9
        )

        with pytest.raises(DailyCastError) as error:
            asyncio.run(
                AIEditorialService(factory, provider).generate_metadata(
                    script,
                    ["事件 metadata-provenance"],
                    estimated_duration_seconds=120,
                    task_run_id=task_run_id,
                    task_step_id=task_step_id,
                    budget=BudgetController(),
                )
            )

        assert error.value.code == "AI_RESPONSE_SCHEMA_INVALID"
        assert artifact_count(factory) == 0
    finally:
        factory.kw["bind"].dispose()
