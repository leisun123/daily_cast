"""Test-first traceability and cache-contract coverage for structured EpisodeScript output."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
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
from dailycast.llm.prompts import PromptTemplate


@pytest.fixture
def migrated_session_factory(app_config_path: Path) -> sessionmaker[Session]:
    """Provide a separate migrated SQLite database for strict-script contract assertions."""
    factory = upgraded_session_factory(app_config_path)
    try:
        yield factory
    finally:
        factory.kw["bind"].dispose()


def _generate(
    factory: sessionmaker[Session],
    provider: FakeLLMProvider,
    payload: dict[str, object],
) -> None:
    fixture = create_selected_event(
        factory,
        key=f"contract-{artifact_count(factory)}",
        content="证据 2026",
    )
    outline = build_outline(fixture.event_id)
    dossiers = build_dossiers(factory, fixture)
    provider._responses[LLMOperation.GENERATE_SCRIPT] = [payload]
    task_run_id, task_step_id = create_task_provenance(factory)
    service = AIEditorialService(factory, provider)
    asyncio.run(
        service.generate_script(
            outline,
            dossiers,
            task_run_id=task_run_id,
            task_step_id=task_step_id,
            budget=BudgetController(),
        )
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload, event_id, article_id: payload["sections"].__setitem__(
            1, {**payload["sections"][1], "event_ids": [event_id + 99]}
        ),
        lambda payload, event_id, article_id: payload["sections"].__setitem__(
            1, {**payload["sections"][1], "article_ids": [article_id + 99]}
        ),
        lambda payload, event_id, article_id: payload.__setitem__(
            "sections", [payload["sections"][1], payload["sections"][0], payload["sections"][2]]
        ),
        lambda payload, event_id, article_id: payload.__setitem__(
            "sections", [payload["sections"][0], payload["sections"][1], payload["sections"][1]]
        ),
    ],
)
def test_invalid_script_references_or_outline_structure_are_not_cached(
    migrated_session_factory: sessionmaker[Session],
    mutate: Callable[[dict[str, object], int, int], None],
) -> None:
    """Unknown IDs, order changes, and duplicate sections fail local schema validation pre-cache."""
    fixture = create_selected_event(
        migrated_session_factory,
        key="invalid-script",
        content="已验证证据 2026",
    )
    outline = build_outline(fixture.event_id)
    payload = valid_script_payload(outline, fixture)
    mutate(payload, fixture.event_id, fixture.article_id)
    provider = FakeLLMProvider({LLMOperation.GENERATE_SCRIPT: [payload]})
    task_run_id, task_step_id = create_task_provenance(migrated_session_factory)
    service = AIEditorialService(migrated_session_factory, provider)

    with pytest.raises(DailyCastError):
        asyncio.run(
            service.generate_script(
                outline,
                build_dossiers(migrated_session_factory, fixture),
                task_run_id=task_run_id,
                task_step_id=task_step_id,
                budget=BudgetController(),
            )
        )

    assert artifact_count(migrated_session_factory) == 0


def test_script_cache_hit_avoids_second_provider_call(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """An exact outline-and-dossier request reuses its validated EpisodeScript across TaskRuns."""
    fixture = create_selected_event(
        migrated_session_factory,
        key="script-cache",
        content="已验证证据 2026",
    )
    outline = build_outline(fixture.event_id)
    payload = valid_script_payload(outline, fixture)
    provider = FakeLLMProvider({LLMOperation.GENERATE_SCRIPT: [payload]})
    service = AIEditorialService(migrated_session_factory, provider)
    dossiers = build_dossiers(migrated_session_factory, fixture)
    first_run_id, first_step_id = create_task_provenance(migrated_session_factory)
    second_run_id, second_step_id = create_task_provenance(migrated_session_factory)

    asyncio.run(
        service.generate_script(
            outline,
            dossiers,
            task_run_id=first_run_id,
            task_step_id=first_step_id,
            budget=BudgetController(),
        )
    )
    reused = asyncio.run(
        service.generate_script(
            outline,
            dossiers,
            task_run_id=second_run_id,
            task_step_id=second_step_id,
            budget=BudgetController(),
        )
    )

    assert reused.cache_hit is True
    assert provider.calls == 1


def test_script_prompt_version_change_causes_cache_miss(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """Prompt version changes the Artifact identity for unchanged outline and evidence."""
    fixture = create_selected_event(
        migrated_session_factory,
        key="script-prompt-cache",
        content="已验证证据 2026",
    )
    outline = build_outline(fixture.event_id)
    payload = valid_script_payload(outline, fixture)
    provider = FakeLLMProvider({LLMOperation.GENERATE_SCRIPT: [payload, payload]})
    dossiers = build_dossiers(migrated_session_factory, fixture)
    first_run_id, first_step_id = create_task_provenance(migrated_session_factory)
    second_run_id, second_step_id = create_task_provenance(migrated_session_factory)
    default_service = AIEditorialService(migrated_session_factory, provider)
    changed_service = AIEditorialService(
        migrated_session_factory,
        provider,
        script_prompt=PromptTemplate(
            version="generate_script_v2",
            system_instruction="Return JSON.",
        ),
    )

    asyncio.run(
        default_service.generate_script(
            outline,
            dossiers,
            task_run_id=first_run_id,
            task_step_id=first_step_id,
            budget=BudgetController(),
        )
    )
    changed = asyncio.run(
        changed_service.generate_script(
            outline,
            dossiers,
            task_run_id=second_run_id,
            task_step_id=second_step_id,
            budget=BudgetController(),
        )
    )

    assert changed.cache_hit is False
    assert provider.calls == 2
