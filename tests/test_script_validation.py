"""Test-first deterministic script-validation behavior for Sprint 4B-3."""

from __future__ import annotations

from pathlib import Path

import pytest
from editorial_test_support import (
    FakeLLMProvider,
    build_dossiers,
    build_outline,
    create_selected_event,
    upgraded_session_factory,
    valid_script_payload,
)
from sqlalchemy.orm import Session, sessionmaker

from dailycast.llm.editorial_service import AIEditorialService
from dailycast.llm.script_schemas import EpisodeScript


@pytest.fixture
def migrated_session_factory(app_config_path: Path) -> sessionmaker[Session]:
    """Provide a real migrated SQLite database for deterministic validation tests."""
    factory = upgraded_session_factory(app_config_path)
    try:
        yield factory
    finally:
        factory.kw["bind"].dispose()


def test_validator_reports_claim_without_article_source(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """An otherwise traceable claim without Article support receives a stable blocking issue."""
    fixture = create_selected_event(
        migrated_session_factory,
        key="claim-without-source",
        content="已验证证据 2026",
    )
    outline = build_outline(fixture.event_id)
    dossiers = build_dossiers(migrated_session_factory, fixture)
    payload = valid_script_payload(outline, fixture)
    payload["sections"][1]["claims"] = [{"text": "没有来源的主张。", "article_ids": []}]
    script = EpisodeScript.model_validate(
        payload,
        context={"outline": outline, "evidence_dossiers": dossiers},
    )
    service = AIEditorialService(migrated_session_factory, FakeLLMProvider({}))

    report = service.validate_script(script, outline, dossiers)

    claim_issue = next(issue for issue in report.issues if issue.code == "CLAIM_WITHOUT_SOURCE")
    assert claim_issue.severity == "blocking"
