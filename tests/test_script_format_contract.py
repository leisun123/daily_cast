"""Unsafe spoken-script formatting must fail before LLMArtifact persistence."""

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


@pytest.mark.parametrize(
    "unsafe_text", ["<p>HTML</p>", "api_key=secret", "task_run_id: 1234", "# 标题"]
)
def test_unsafe_script_output_is_never_cached(app_config_path: Path, unsafe_text: str) -> None:
    """Unsafe provider output fails before LLMArtifact insertion."""
    factory: sessionmaker[Session] = upgraded_session_factory(app_config_path)
    try:
        fixture = create_selected_event(
            factory, key=f"format-{unsafe_text[:3]}", content="可信证据。"
        )
        outline = build_outline(fixture.event_id)
        payload = valid_script_payload(outline, fixture, text=unsafe_text)
        provider = FakeLLMProvider({LLMOperation.GENERATE_SCRIPT: [payload]})
        task_run_id, task_step_id = create_task_provenance(factory)

        with pytest.raises(DailyCastError) as error:
            asyncio.run(
                AIEditorialService(factory, provider).generate_script(
                    outline,
                    build_dossiers(factory, fixture),
                    task_run_id=task_run_id,
                    task_step_id=task_step_id,
                    budget=BudgetController(),
                )
            )

        assert error.value.code == "AI_RESPONSE_SCHEMA_INVALID"
        assert artifact_count(factory) == 0
    finally:
        factory.kw["bind"].dispose()
