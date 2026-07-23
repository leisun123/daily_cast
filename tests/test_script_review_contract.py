"""Strict review-schema and Artifact persistence tests."""

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


@pytest.fixture
def migrated_session_factory(app_config_path: Path) -> sessionmaker[Session]:
    """Provide an upgraded real SQLite database for strict review tests."""
    factory = upgraded_session_factory(app_config_path)
    try:
        yield factory
    finally:
        factory.kw["bind"].dispose()


@pytest.mark.parametrize(
    "review_payload",
    [
        {
            "schema_version": "1",
            "verdict": "revise",
            "issues": [
                {
                    "severity": "warning",
                    "type": "unsupported_claim",
                    "section_id": "unknown-section",
                    "message": "Unknown section.",
                    "article_ids": [],
                }
            ],
            "suggested_changes": [],
        },
        {
            "schema_version": "1",
            "verdict": "revise",
            "issues": [
                {
                    "severity": "warning",
                    "type": "unsupported_claim",
                    "section_id": "news-1",
                    "message": "Unknown source.",
                    "article_ids": [999999],
                }
            ],
            "suggested_changes": [],
        },
        {
            "schema_version": "1",
            "verdict": "pass",
            "issues": [
                {
                    "severity": "blocking",
                    "type": "unsupported_claim",
                    "section_id": "news-1",
                    "message": "Cannot pass with this blocker.",
                    "article_ids": [],
                }
            ],
            "suggested_changes": [],
        },
    ],
)
def test_invalid_review_is_not_stored_as_an_artifact(
    migrated_session_factory: sessionmaker[Session], review_payload: dict[str, object]
) -> None:
    """Unknown review references or an inconsistent verdict never enter LLMArtifact storage."""
    fixture = create_selected_event(
        migrated_session_factory,
        key=f"review-contract-{review_payload['verdict']}-{len(review_payload['issues'])}",
        content="有据可查的新闻证据。",
    )
    outline = build_outline(fixture.event_id)
    dossiers = build_dossiers(migrated_session_factory, fixture)
    script = EpisodeScript.model_validate(
        valid_script_payload(outline, fixture),
        context={"outline": outline, "evidence_dossiers": dossiers},
    )
    provider = FakeLLMProvider({LLMOperation.REVIEW_SCRIPT: [review_payload]})
    task_run_id, task_step_id = create_task_provenance(
        migrated_session_factory, step_name="checking", step_order=9
    )

    with pytest.raises(DailyCastError) as error:
        asyncio.run(
            AIEditorialService(migrated_session_factory, provider).review_script(
                script,
                dossiers,
                task_run_id=task_run_id,
                task_step_id=task_step_id,
                budget=BudgetController(),
            )
        )

    assert error.value.code == "AI_RESPONSE_SCHEMA_INVALID"
    assert artifact_count(migrated_session_factory) == 0
