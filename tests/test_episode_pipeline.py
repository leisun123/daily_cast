"""Pipeline integration tests for the Sprint 5A create_episode checkpoint."""

from __future__ import annotations

import asyncio
import json

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

from dailycast.core.time import Clock
from dailycast.db.repositories import EpisodeRepository, TaskRunRepository
from dailycast.db.transactions import UnitOfWork
from dailycast.episodes.service import EpisodeService
from dailycast.llm.editorial_service import AIEditorialService
from dailycast.llm.script_schemas import EpisodeScript, ScriptReview
from dailycast.pipeline.context import PipelineContext
from dailycast.pipeline.editorial_artifacts import EditorialArtifactStore
from dailycast.pipeline.steps.create_episode import CreateEpisodeStep


def test_rejected_editorial_checkpoint_creates_no_episode(app_config_path, tmp_path) -> None:
    """A non-pass review result records a skipped checkpoint, never an Episode."""
    factory: sessionmaker[Session] = upgraded_session_factory(app_config_path)
    try:
        fixture = create_selected_event(factory, key="pipeline-rejected", content="可信新闻证据。")
        outline = build_outline(fixture.event_id)
        dossiers = build_dossiers(factory, fixture)
        script = EpisodeScript.model_validate(
            valid_script_payload(outline, fixture),
            context={"outline": outline, "evidence_dossiers": dossiers},
        )
        validation = AIEditorialService(factory, FakeLLMProvider({})).validate_script(
            script, outline, dossiers
        )
        task_run_id, task_step_id = create_task_provenance(
            factory, step_name="create_episode", step_order=10
        )
        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            task_run = TaskRunRepository(unit.session).get(task_run_id)
            assert task_run is not None
            task_run.request_json = json.dumps({"episode_date": "2026-07-22", "edition": "daily"})
            unit.session.flush()
        store = EditorialArtifactStore(tmp_path)
        store.write_json(task_run_id, "outline.json", outline.model_dump(mode="json"))
        store.write_json(task_run_id, "script.json", script.model_dump(mode="json"))
        store.write_json(task_run_id, "validation.json", validation.model_dump(mode="json"))
        store.write_json(
            task_run_id,
            "review.json",
            ScriptReview.model_validate(
                {
                    "schema_version": "1",
                    "verdict": "human_review",
                    "issues": [],
                    "suggested_changes": [],
                },
                context={"script": script, "evidence_dossiers": dossiers},
            ).model_dump(mode="json"),
        )
        context = PipelineContext(
            task_run_id=task_run_id,
            session_factory=factory,
            shutdown_requested=asyncio.Event(),
            clock=Clock(),
            values={
                "active_task_step_id": task_step_id,
                "outlined_news_event_ids": (fixture.event_id,),
                "evidence_dossiers": dossiers,
            },
        )

        result = asyncio.run(CreateEpisodeStep(EpisodeService(factory), tmp_path).run(context))

        assert result.output_count == 0
        assert result.warning_count == 1
        assert result.details["skip_reason"] == "EDITORIAL_REVIEW_NOT_PASS"
        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            assert EpisodeRepository(unit.session).list() == []
    finally:
        factory.kw["bind"].dispose()
