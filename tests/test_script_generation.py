"""Test-first coverage for Sprint 4B-3 structured script generation."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from editorial_test_support import (
    FakeLLMProvider,
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
from dailycast.llm.prompts.generate_script_v3 import GENERATE_SCRIPT_V3
from dailycast.llm.script_editorial import _script_messages


@pytest.fixture
def migrated_session_factory(app_config_path: Path) -> sessionmaker[Session]:
    """Provide a real migrated SQLite database for Artifact-backed script tests."""
    factory = upgraded_session_factory(app_config_path)
    try:
        yield factory
    finally:
        factory.kw["bind"].dispose()


def test_generate_script_returns_traceable_structured_script(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """A valid bounded outline and dossier produce a traceable Chinese EpisodeScript artifact."""
    full_article = "FULL_ARTICLE_CONTENT_MUST_NOT_BE_SENT " * 100
    fixture = create_selected_event(
        migrated_session_factory,
        key="valid-script",
        content=full_article,
    )
    outline = build_outline(fixture.event_id)
    dossiers = build_dossiers(migrated_session_factory, fixture)
    provider = FakeLLMProvider(
        {LLMOperation.GENERATE_SCRIPT: [valid_script_payload(outline, fixture)]}
    )
    service = AIEditorialService(migrated_session_factory, provider)
    task_run_id, task_step_id = create_task_provenance(migrated_session_factory)

    result = asyncio.run(
        service.generate_script(
            outline,
            dossiers,
            task_run_id=task_run_id,
            task_step_id=task_step_id,
            budget=BudgetController(),
        )
    )

    assert result.script.sections[1].section_id == "news-1"
    assert result.script.sections[1].article_ids == (fixture.article_id,)
    assert full_article not in canonical_messages(provider)


def test_script_request_exposes_exact_reference_allowlists_for_json_only_providers(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """The model receives exact section order and IDs instead of inferring cross-field rules."""
    fixture = create_selected_event(
        migrated_session_factory,
        key="script-contract",
        content="Bounded supporting evidence.",
    )
    outline = build_outline(fixture.event_id)
    dossiers = build_dossiers(migrated_session_factory, fixture)

    messages = _script_messages(outline, dossiers, GENERATE_SCRIPT_V3)
    payload = json.loads(messages[1].content)
    constraints = payload["output_constraints"]

    assert GENERATE_SCRIPT_V3.version == "generate_script_v4"
    assert "DailyCast" in messages[0].content
    assert "varies wording from episode to episode" in messages[0].content
    assert "大家好，欢迎收听DailyCast。" not in messages[0].content
    assert "为什么值得关注" in messages[0].content
    assert "target_seconds" in messages[0].content
    assert "每个 section 的 seconds" in messages[0].content
    assert constraints["required_section_ids"] == [
        section.section_id for section in outline.sections
    ]
    news_constraints = constraints["allowed_references_by_section"][1]
    assert news_constraints["allowed_event_ids"] == [fixture.event_id]
    assert news_constraints["allowed_article_ids"] == [fixture.article_id]
