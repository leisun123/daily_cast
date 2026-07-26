"""Controlled one-shot revision and final checking workflow tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

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
from dailycast.llm.script_checking import ScriptCheckingService
from dailycast.llm.script_schemas import EpisodeScript


def _script(
    outline: object, fixture: object, dossiers: object, *, text: str | None = None
) -> EpisodeScript:
    """Build one traceable script with enough spoken characters for the fixed outline duration."""
    return EpisodeScript.model_validate(
        valid_script_payload(outline, fixture, text=text),
        context={"outline": outline, "evidence_dossiers": dossiers},
    )


def _metadata_payload() -> dict[str, object]:
    """Return a strict plain-text metadata response used by successful checking scenarios."""
    return {
        "schema_version": "1",
        "title": "今日科技新闻",
        "description": "围绕一项经过核验的科技新闻展开。",
        "keywords": ["科技", "新闻"],
    }


def _revise_payload(fixture: object, *, text: str) -> dict[str, object]:
    """Create a revision response preserving the trusted IDs and outline order."""
    return {
        "schema_version": "1",
        "sections": [
            {
                "section_id": "intro",
                "text": "欢迎收听今天的 DailyCast。",
                "event_ids": [],
                "article_ids": [],
                "claims": [],
            },
            {
                "section_id": "news-1",
                "text": text,
                "event_ids": [fixture.event_id],
                "article_ids": [fixture.article_id],
                "claims": [{"text": "事件正在持续发展。", "article_ids": [fixture.article_id]}],
            },
            {
                "section_id": "outro",
                "text": "以上就是今天的节目，感谢收听。",
                "event_ids": [],
                "article_ids": [],
                "claims": [],
            },
        ],
        "pronunciation_hints": [],
    }


def _review(verdict: str, fixture: object, *, blocking: bool = False) -> dict[str, object]:
    """Return a bounded review payload pointing only at trusted script evidence."""
    severity = "blocking" if blocking else "warning"
    return {
        "schema_version": "1",
        "verdict": verdict,
        "issues": (
            [
                {
                    "severity": severity,
                    "type": "spoken_style",
                    "section_id": "news-1",
                    "message": "请让这段更自然。",
                    "article_ids": [fixture.article_id],
                }
            ]
            if verdict != "pass"
            else []
        ),
        "suggested_changes": ["只处理已报告的问题。"] if verdict == "revise" else [],
    }


def test_one_controlled_revision_can_produce_accepted_metadata(app_config_path: Path) -> None:
    """Checking performs one revision, revalidates and rereviews it, then creates metadata."""
    factory: sessionmaker[Session] = upgraded_session_factory(app_config_path)
    try:
        fixture = create_selected_event(factory, key="revision-success", content="可信新闻证据。")
        outline = build_outline(fixture.event_id)
        dossiers = build_dossiers(factory, fixture)
        initial = _script(outline, fixture, dossiers)
        revised_text = "修订后的中文播报稿，保持事实准确且更自然。" * 20
        provider = FakeLLMProvider(
            {
                LLMOperation.REVIEW_SCRIPT: [
                    _review("revise", fixture),
                    _review("pass", fixture),
                ],
                LLMOperation.GENERATE_SCRIPT: [_revise_payload(fixture, text=revised_text)],
                LLMOperation.GENERATE_METADATA: [_metadata_payload()],
            }
        )
        task_run_id, task_step_id = create_task_provenance(
            factory, step_name="checking", step_order=9
        )

        result = asyncio.run(
            ScriptCheckingService(AIEditorialService(factory, provider)).check(
                initial,
                outline,
                dossiers,
                selected_event_titles=["事件 revision-success"],
                task_run_id=task_run_id,
                task_step_id=task_step_id,
                budget=BudgetController(),
            )
        )

        assert result.automatic_revision_count == 1
        assert not result.requires_human_review
        assert result.metadata is not None
        assert result.review.verdict == "pass"
        assert provider.calls_by_operation[LLMOperation.GENERATE_SCRIPT] == 1
        assert provider.calls_by_operation[LLMOperation.REVIEW_SCRIPT] == 2
        assert provider.calls_by_operation[LLMOperation.GENERATE_METADATA] == 1
    finally:
        factory.kw["bind"].dispose()


def test_automatic_revision_never_runs_more_than_once(app_config_path: Path) -> None:
    """A second revise verdict returns a human-review result instead of another model revision."""
    factory: sessionmaker[Session] = upgraded_session_factory(app_config_path)
    try:
        fixture = create_selected_event(factory, key="revision-once", content="可信新闻证据。")
        outline = build_outline(fixture.event_id)
        dossiers = build_dossiers(factory, fixture)
        provider = FakeLLMProvider(
            {
                LLMOperation.REVIEW_SCRIPT: [
                    _review("revise", fixture),
                    _review("revise", fixture),
                ],
                LLMOperation.GENERATE_SCRIPT: [
                    _revise_payload(fixture, text="经一次修订后的中文播报稿。" * 28)
                ],
            }
        )
        task_run_id, task_step_id = create_task_provenance(
            factory, step_name="checking", step_order=9
        )

        result = asyncio.run(
            ScriptCheckingService(AIEditorialService(factory, provider)).check(
                _script(outline, fixture, dossiers),
                outline,
                dossiers,
                selected_event_titles=["事件 revision-once"],
                task_run_id=task_run_id,
                task_step_id=task_step_id,
                budget=BudgetController(),
            )
        )

        assert result.automatic_revision_count == 1
        assert result.requires_human_review
        assert result.metadata is None
        assert provider.calls_by_operation[LLMOperation.GENERATE_SCRIPT] == 1
        assert provider.calls_by_operation[LLMOperation.REVIEW_SCRIPT] == 2
        assert LLMOperation.GENERATE_METADATA not in provider.calls_by_operation
    finally:
        factory.kw["bind"].dispose()


def test_deterministic_blocker_requires_human_review_without_metadata(
    app_config_path: Path,
) -> None:
    """A local blocker stays visible and cannot be hidden by metadata generation."""
    factory: sessionmaker[Session] = upgraded_session_factory(app_config_path)
    try:
        fixture = create_selected_event(
            factory, key="deterministic-blocker", content="可信新闻证据。"
        )
        outline = build_outline(fixture.event_id)
        dossiers = build_dossiers(factory, fixture)
        payload = valid_script_payload(outline, fixture)
        sections = payload["sections"]
        assert isinstance(sections, list)
        claims = sections[1]["claims"]
        assert isinstance(claims, list)
        claims[0]["article_ids"] = []
        script = EpisodeScript.model_validate(
            payload,
            context={"outline": outline, "evidence_dossiers": dossiers},
        )
        provider = FakeLLMProvider({LLMOperation.REVIEW_SCRIPT: [_review("pass", fixture)]})
        task_run_id, task_step_id = create_task_provenance(
            factory, step_name="checking", step_order=9
        )

        result = asyncio.run(
            ScriptCheckingService(AIEditorialService(factory, provider)).check(
                script,
                outline,
                dossiers,
                selected_event_titles=["事件 deterministic-blocker"],
                task_run_id=task_run_id,
                task_step_id=task_step_id,
                budget=BudgetController(),
            )
        )

        assert result.requires_human_review
        assert result.metadata is None
        assert any(issue.code == "CLAIM_WITHOUT_SOURCE" for issue in result.validation.issues)
    finally:
        factory.kw["bind"].dispose()


def test_relaxed_quality_gate_keeps_short_revise_artifacts_without_metadata(
    app_config_path: Path,
) -> None:
    """A too-short script is never allowed to proceed to episode metadata."""
    factory: sessionmaker[Session] = upgraded_session_factory(app_config_path)
    try:
        fixture = create_selected_event(
            factory, key="alpha-relaxed-checking", content="可信新闻证据。"
        )
        outline = build_outline(fixture.event_id)
        dossiers = build_dossiers(factory, fixture)
        provider = FakeLLMProvider(
            {
                LLMOperation.REVIEW_SCRIPT: [_review("revise", fixture)],
                LLMOperation.GENERATE_METADATA: [_metadata_payload()],
            }
        )
        task_run_id, task_step_id = create_task_provenance(
            factory, step_name="checking", step_order=9
        )

        result = asyncio.run(
            ScriptCheckingService(
                AIEditorialService(factory, provider),
                max_automatic_script_revisions=0,
                enforce_quality_gate=False,
            ).check(
                _script(outline, fixture, dossiers, text="过短的播报稿。"),
                outline,
                dossiers,
                selected_event_titles=["事件 alpha-relaxed-checking"],
                task_run_id=task_run_id,
                task_step_id=task_step_id,
                budget=BudgetController(),
            )
        )

        assert result.requires_human_review is True
        assert result.review.verdict == "revise"
        assert any(issue.code == "SCRIPT_TOO_SHORT" for issue in result.validation.issues)
        assert result.automatic_revision_count == 0
        assert result.metadata is None
        assert LLMOperation.GENERATE_SCRIPT not in provider.calls_by_operation
        assert provider.calls_by_operation[LLMOperation.REVIEW_SCRIPT] == 1
        assert LLMOperation.GENERATE_METADATA not in provider.calls_by_operation
    finally:
        factory.kw["bind"].dispose()


def test_relaxed_quality_gate_revises_a_short_script_before_metadata(
    app_config_path: Path,
) -> None:
    """Length blockers trigger the single allowed revision even in relaxed Alpha mode."""
    factory: sessionmaker[Session] = upgraded_session_factory(app_config_path)
    try:
        fixture = create_selected_event(
            factory, key="short-script-revision", content="可信新闻证据。"
        )
        outline = build_outline(fixture.event_id)
        dossiers = build_dossiers(factory, fixture)
        provider = FakeLLMProvider(
            {
                LLMOperation.REVIEW_SCRIPT: [
                    _review("pass", fixture),
                    _review("pass", fixture),
                ],
                LLMOperation.GENERATE_SCRIPT: [
                    _revise_payload(fixture, text="修订后的中文播报稿。" * 50)
                ],
                LLMOperation.GENERATE_METADATA: [_metadata_payload()],
            }
        )
        task_run_id, task_step_id = create_task_provenance(
            factory, step_name="checking", step_order=9
        )

        result = asyncio.run(
            ScriptCheckingService(
                AIEditorialService(factory, provider),
                max_automatic_script_revisions=1,
                enforce_quality_gate=False,
            ).check(
                _script(outline, fixture, dossiers, text="过短的播报稿。"),
                outline,
                dossiers,
                selected_event_titles=["事件 short-script-revision"],
                task_run_id=task_run_id,
                task_step_id=task_step_id,
                budget=BudgetController(),
            )
        )

        assert result.automatic_revision_count == 1
        assert not result.requires_human_review
        assert result.metadata is not None
        assert provider.calls_by_operation[LLMOperation.GENERATE_SCRIPT] == 1
        assert provider.calls_by_operation[LLMOperation.REVIEW_SCRIPT] == 2
        assert provider.calls_by_operation[LLMOperation.GENERATE_METADATA] == 1
    finally:
        factory.kw["bind"].dispose()


def test_relaxed_quality_gate_publishes_a_long_script_despite_style_revision(
    app_config_path: Path,
) -> None:
    """Alpha mode keeps semantic style feedback visible without blocking publication."""
    factory: sessionmaker[Session] = upgraded_session_factory(app_config_path)
    try:
        fixture = create_selected_event(
            factory, key="alpha-relaxed-style", content="可信新闻证据。"
        )
        outline = build_outline(fixture.event_id)
        dossiers = build_dossiers(factory, fixture)
        provider = FakeLLMProvider(
            {
                LLMOperation.REVIEW_SCRIPT: [_review("revise", fixture)],
                LLMOperation.GENERATE_METADATA: [_metadata_payload()],
            }
        )
        task_run_id, task_step_id = create_task_provenance(
            factory, step_name="checking", step_order=9
        )

        result = asyncio.run(
            ScriptCheckingService(
                AIEditorialService(factory, provider),
                enforce_quality_gate=False,
            ).check(
                _script(outline, fixture, dossiers),
                outline,
                dossiers,
                selected_event_titles=["事件 alpha-relaxed-style"],
                task_run_id=task_run_id,
                task_step_id=task_step_id,
                budget=BudgetController(),
            )
        )

        assert not result.validation.has_blocking_issues
        assert not result.requires_human_review
        assert result.metadata is not None
        assert result.automatic_revision_count == 0
        assert provider.calls_by_operation[LLMOperation.GENERATE_METADATA] == 1
    finally:
        factory.kw["bind"].dispose()


def test_strict_quality_gate_keeps_short_revise_artifacts_without_metadata(
    app_config_path: Path,
) -> None:
    """The same content-quality findings remain a strict-mode metadata gate."""
    factory: sessionmaker[Session] = upgraded_session_factory(app_config_path)
    try:
        fixture = create_selected_event(
            factory, key="alpha-strict-checking", content="可信新闻证据。"
        )
        outline = build_outline(fixture.event_id)
        dossiers = build_dossiers(factory, fixture)
        provider = FakeLLMProvider({LLMOperation.REVIEW_SCRIPT: [_review("revise", fixture)]})
        task_run_id, task_step_id = create_task_provenance(
            factory, step_name="checking", step_order=9
        )

        result = asyncio.run(
            ScriptCheckingService(
                AIEditorialService(factory, provider),
                max_automatic_script_revisions=0,
                enforce_quality_gate=True,
            ).check(
                _script(outline, fixture, dossiers, text="过短的播报稿。"),
                outline,
                dossiers,
                selected_event_titles=["事件 alpha-strict-checking"],
                task_run_id=task_run_id,
                task_step_id=task_step_id,
                budget=BudgetController(),
            )
        )

        assert result.requires_human_review is True
        assert result.metadata is None
        assert LLMOperation.GENERATE_METADATA not in provider.calls_by_operation
    finally:
        factory.kw["bind"].dispose()
