"""Script text must not carry provider credentials or task-runtime metadata."""

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


@pytest.mark.parametrize("leaked_text", ["api_key=secret", "task_run_id: 1234"])
def test_validator_blocks_secret_or_task_metadata_leakage(
    app_config_path: Path, leaked_text: str
) -> None:
    """Sensitive runtime details cannot become spoken script text or reviewable output artifacts."""
    factory: sessionmaker[Session] = upgraded_session_factory(app_config_path)
    try:
        fixture = create_selected_event(
            factory, key=f"leak-{leaked_text[:4]}", content="可信证据。"
        )
        outline = build_outline(fixture.event_id)
        dossiers = build_dossiers(factory, fixture)
        script = EpisodeScript.model_validate(
            valid_script_payload(outline, fixture),
            context={"outline": outline, "evidence_dossiers": dossiers},
        )
        sections = list(script.sections)
        sections[1] = sections[1].model_copy(update={"text": leaked_text})
        script = script.model_copy(update={"sections": tuple(sections)})

        report = ScriptValidator(
            estimated_chars_per_second=4.0,
            duration_tolerance_ratio=0.2,
            max_script_chars=12_000,
            max_section_chars=2_400,
        ).validate(script, outline, dossiers)

        assert any(
            issue.code == "UNSUPPORTED_FORMAT" and issue.severity == "blocking"
            for issue in report.issues
        )
    finally:
        factory.kw["bind"].dispose()
