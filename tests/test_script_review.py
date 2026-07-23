"""Test-first bounded LLM review behavior for a validated EpisodeScript."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from editorial_test_support import (
    FakeLLMProvider,
    build_dossiers,
    build_outline,
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


@pytest.fixture
def migrated_session_factory(app_config_path: Path) -> sessionmaker[Session]:
    """Provide a real migrated SQLite database for Artifact-backed script-review tests."""
    factory = upgraded_session_factory(app_config_path)
    try:
        yield factory
    finally:
        factory.kw["bind"].dispose()


def test_review_script_returns_valid_evidence_bounded_review(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """A valid review response is schema-checked through the Artifact cache layer."""
    fixture = create_selected_event(
        migrated_session_factory,
        key="review-pass",
        content="有据可查的新闻证据。",
    )
    outline = build_outline(fixture.event_id)
    dossiers = build_dossiers(migrated_session_factory, fixture)
    script = EpisodeScript.model_validate(
        valid_script_payload(outline, fixture),
        context={"outline": outline, "evidence_dossiers": dossiers},
    )
    provider = FakeLLMProvider(
        {
            LLMOperation.REVIEW_SCRIPT: [
                {
                    "schema_version": "1",
                    "verdict": "pass",
                    "issues": [],
                    "suggested_changes": [],
                }
            ]
        }
    )
    task_run_id, task_step_id = create_task_provenance(
        migrated_session_factory, step_name="checking", step_order=9
    )
    service = AIEditorialService(migrated_session_factory, provider)

    result = asyncio.run(
        service.review_script(
            script,
            dossiers,
            task_run_id=task_run_id,
            task_step_id=task_step_id,
            budget=BudgetController(),
        )
    )

    assert result.review.verdict == "pass"
