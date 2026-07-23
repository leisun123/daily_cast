"""Focused deterministic formatting, numeric, and duration validation rules."""

from __future__ import annotations

from pathlib import Path

import pytest
from editorial_test_support import (
    build_dossiers,
    build_outline,
    create_selected_event,
    upgraded_session_factory,
    valid_script_payload,
)
from sqlalchemy.orm import Session, sessionmaker

from dailycast.llm.script_schemas import EpisodeScript
from dailycast.llm.script_validation import ScriptValidator


@pytest.fixture
def migrated_session_factory(app_config_path: Path) -> sessionmaker[Session]:
    """Provide a real migrated database for local ScriptValidator rule tests."""
    factory = upgraded_session_factory(app_config_path)
    try:
        yield factory
    finally:
        factory.kw["bind"].dispose()


def _validated_script(
    factory: sessionmaker[Session], *, text: str
) -> tuple[EpisodeScript, object, tuple[object, ...]]:
    fixture = create_selected_event(
        factory, key=f"rules-{text[:8]}", content="证据中包含数字 2026。"
    )
    outline = build_outline(fixture.event_id)
    dossiers = build_dossiers(factory, fixture)
    script = EpisodeScript.model_validate(
        valid_script_payload(outline, fixture),
        context={"outline": outline, "evidence_dossiers": dossiers},
    )
    sections = list(script.sections)
    sections[1] = sections[1].model_copy(update={"text": text})
    return script.model_copy(update={"sections": tuple(sections)}), outline, dossiers


@pytest.mark.parametrize(
    "text", ["# 一级标题", "<p>HTML 不可播报</p>", "原始链接 https://example.test"]
)
def test_validator_detects_unsupported_spoken_format(
    migrated_session_factory: sessionmaker[Session], text: str
) -> None:
    """Markdown, raw HTML, and URLs are blocking unsupported spoken-text constructs."""
    script, outline, dossiers = _validated_script(migrated_session_factory, text=text)
    validator = ScriptValidator(
        estimated_chars_per_second=4.0,
        duration_tolerance_ratio=0.2,
        max_script_chars=12_000,
        max_section_chars=2_400,
    )

    report = validator.validate(script, outline, dossiers)

    assert any(
        issue.code == "UNSUPPORTED_FORMAT" and issue.severity == "blocking"
        for issue in report.issues
    )


def test_validator_warns_when_number_is_absent_from_referenced_evidence(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """Numeric checking warns conservatively without declaring the underlying spoken claim false."""
    script, outline, dossiers = _validated_script(
        migrated_session_factory,
        text="报道提到 9999 位参与者，这是一项需要继续观察的变化。",
    )
    validator = ScriptValidator(
        estimated_chars_per_second=4.0,
        duration_tolerance_ratio=0.2,
        max_script_chars=12_000,
        max_section_chars=2_400,
    )

    report = validator.validate(script, outline, dossiers)

    number_issue = next(
        issue for issue in report.issues if issue.code == "NUMBER_NOT_FOUND_IN_EVIDENCE"
    )
    assert number_issue.severity == "warning"


def test_validator_reports_estimated_duration_outside_tolerance(
    migrated_session_factory: sessionmaker[Session],
) -> None:
    """Estimated duration uses normalized characters and yields a stable short-script issue."""
    script, outline, dossiers = _validated_script(migrated_session_factory, text="很短。")
    validator = ScriptValidator(
        estimated_chars_per_second=1.0,
        duration_tolerance_ratio=0.2,
        max_script_chars=12_000,
        max_section_chars=2_400,
    )

    report = validator.validate(script, outline, dossiers)

    assert any(issue.code == "SCRIPT_TOO_SHORT" for issue in report.issues)
