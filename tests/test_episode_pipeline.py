"""Pipeline integration tests for the Sprint 5A create_episode checkpoint."""

from __future__ import annotations

import asyncio
import json

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
from test_tts_audio import AtomicFakeMerger

from dailycast.core.time import Clock
from dailycast.db.models import EpisodeStatus, PublicationStatus, TaskRunStatus
from dailycast.db.repositories import EpisodeRepository, TaskRunRepository, TaskStepRepository
from dailycast.db.transactions import UnitOfWork
from dailycast.episodes.service import EpisodeService
from dailycast.llm.editorial_service import AIEditorialService
from dailycast.llm.script_schemas import (
    EpisodeMetadata,
    EpisodeScript,
    ScriptReview,
    ValidationReport,
)
from dailycast.pipeline.context import PipelineContext
from dailycast.pipeline.editorial_artifacts import EditorialArtifactStore
from dailycast.pipeline.steps.create_episode import CreateEpisodeStep
from dailycast.pipeline.steps.generate_audio import GenerateAudioStep
from dailycast.pipeline.steps.publish import PublishStep
from dailycast.publishing.rss import RSSPublisher, RSSSettings
from dailycast.publishing.service import PublicationService
from dailycast.tts.providers.fake import FakeTTSProvider
from dailycast.tts.service import AudioGenerationService, TTSGenerationSettings


@pytest.mark.parametrize(
    ("review_verdict", "validation_issues", "skip_reason"),
    [
        pytest.param("human_review", (), "EDITORIAL_REVIEW_NOT_PASS", id="review"),
        pytest.param(
            "pass",
            (
                {
                    "code": "UNSUPPORTED_CLAIM",
                    "severity": "blocking",
                    "section_id": "news-1",
                    "message": "需要人工修订该播报主张。",
                    "related_article_ids": (),
                },
            ),
            "SCRIPT_VALIDATION_FAILED",
            id="validation",
        ),
    ],
)
def test_ineligible_editorial_checkpoint_stops_before_episode(
    app_config_path,
    tmp_path,
    review_verdict: str,
    validation_issues: tuple[dict[str, object], ...],
    skip_reason: str,
) -> None:
    """Every eligible-to-skip editorial result stops before Episode-dependent checkpoints."""
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
        if validation_issues:
            validation = ValidationReport.model_validate(
                {
                    "schema_version": "1",
                    "estimated_duration_seconds": validation.estimated_duration_seconds,
                    "character_count": validation.character_count,
                    "issues": validation_issues,
                }
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
                    "verdict": review_verdict,
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
        assert result.details["skip_reason"] == skip_reason
        assert result.stop_pipeline is True
        assert result.terminal_status is TaskRunStatus.WAITING_ACTION
        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            assert EpisodeRepository(unit.session).list() == []
    finally:
        factory.kw["bind"].dispose()


@pytest.mark.parametrize(
    ("enforce_quality_gate", "episode_created"),
    [
        pytest.param(False, False, id="alpha-relaxed"),
        pytest.param(True, False, id="strict"),
    ],
)
def test_short_script_artifacts_never_create_an_episode(
    app_config_path,
    tmp_path,
    enforce_quality_gate: bool,
    episode_created: bool,
) -> None:
    """A duration blocker stays auditable but must never create a publishable Episode."""
    factory: sessionmaker[Session] = upgraded_session_factory(app_config_path)
    try:
        fixture = create_selected_event(
            factory,
            key=f"quality-{enforce_quality_gate}",
            content="可信新闻证据。",
        )
        outline = build_outline(fixture.event_id)
        dossiers = build_dossiers(factory, fixture)
        script = EpisodeScript.model_validate(
            valid_script_payload(outline, fixture),
            context={"outline": outline, "evidence_dossiers": dossiers},
        )
        validation = ValidationReport.model_validate(
            {
                "schema_version": "1",
                "estimated_duration_seconds": 30,
                "character_count": 120,
                "issues": [
                    {
                        "code": "SCRIPT_TOO_SHORT",
                        "severity": "blocking",
                        "section_id": None,
                        "message": "Estimated spoken duration is below the configured tolerance.",
                        "related_article_ids": [],
                    }
                ],
            }
        )
        review = ScriptReview.model_validate(
            {
                "schema_version": "1",
                "verdict": "revise",
                "issues": [
                    {
                        "severity": "warning",
                        "type": "spoken_style",
                        "section_id": "news-1",
                        "message": "请让这段更自然。",
                        "article_ids": [fixture.article_id],
                    }
                ],
                "suggested_changes": ["只处理已报告的问题。"],
            },
            context={"script": script, "evidence_dossiers": dossiers},
        )
        metadata = EpisodeMetadata.model_validate(
            {
                "schema_version": "1",
                "title": "Alpha 质量标记节目",
                "description": "保留质量检查结果的结构化播报稿。",
                "keywords": ["Alpha", "新闻"],
            },
            context={"script": script, "selected_event_titles": ("质量标记事件",)},
        )
        task_run_id, task_step_id = create_task_provenance(
            factory, step_name="create_episode", step_order=10
        )
        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            task_run = TaskRunRepository(unit.session).get(task_run_id)
            assert task_run is not None
            task_run.request_json = json.dumps({"episode_date": "2026-07-24", "edition": "daily"})
            unit.session.flush()
        store = EditorialArtifactStore(tmp_path)
        store.write_json(task_run_id, "outline.json", outline.model_dump(mode="json"))
        store.write_json(task_run_id, "script.json", script.model_dump(mode="json"))
        store.write_json(task_run_id, "validation.json", validation.model_dump(mode="json"))
        store.write_json(task_run_id, "review.json", review.model_dump(mode="json"))
        store.write_json(task_run_id, "metadata.json", metadata.model_dump(mode="json"))
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

        result = asyncio.run(
            CreateEpisodeStep(
                EpisodeService(factory), tmp_path, enforce_quality_gate=enforce_quality_gate
            ).run(context)
        )

        if not episode_created:
            assert result.output_count == 0
            assert result.stop_pipeline is True
            assert result.terminal_status is TaskRunStatus.WAITING_ACTION
            return

        assert result.output_count == 1
        episode_id = context.values["episode_id"]
        assert isinstance(episode_id, int)
        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            episode = EpisodeRepository(unit.session).get(episode_id)
            assert episode is not None
            saved_review = json.loads(episode.review_json)
            assert saved_review == {
                "validation": validation.model_dump(mode="json"),
                "review": review.model_dump(mode="json"),
            }
            audio_step_id = (
                TaskStepRepository(unit.session)
                .create(
                    task_run_id=task_run_id,
                    step_name="generate_audio",
                    step_order=11,
                    attempt=1,
                    status="running",
                    details_json="{}",
                )
                .id
            )
        audio_service = AudioGenerationService(
            factory,
            FakeTTSProvider(),
            data_dir=tmp_path / "audio-data",
            merger=AtomicFakeMerger(),
            settings=TTSGenerationSettings(voice="zh-CN-XiaoxiaoNeural"),
        )
        audio_result = asyncio.run(
            GenerateAudioStep(audio_service).run(
                PipelineContext(
                    task_run_id=task_run_id,
                    session_factory=factory,
                    shutdown_requested=asyncio.Event(),
                    clock=Clock(),
                    values={"active_task_step_id": audio_step_id, "episode_id": episode_id},
                )
            )
        )
        assert audio_result.output_count == 1
        assert (
            tmp_path / "audio-data" / "audio" / "drafts" / str(episode_id) / "revision-1.mp3"
        ).is_file()
        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            publish_step_id = (
                TaskStepRepository(unit.session)
                .create(
                    task_run_id=task_run_id,
                    step_name="publish",
                    step_order=12,
                    attempt=1,
                    status="running",
                    details_json="{}",
                )
                .id
            )
        publication_result = asyncio.run(
            PublishStep(
                EpisodeService(factory),
                PublicationService(
                    factory,
                    RSSPublisher(
                        data_dir=tmp_path / "audio-data",
                        public_dir=tmp_path / "public",
                        settings=RSSSettings(
                            public_base_url="http://127.0.0.1:8000",
                            feed_title="DailyCast Alpha",
                            feed_description="Alpha output test.",
                            language="zh-CN",
                            author="DailyCast",
                        ),
                    ),
                ),
                auto_publish=True,
            ).run(
                PipelineContext(
                    task_run_id=task_run_id,
                    session_factory=factory,
                    shutdown_requested=asyncio.Event(),
                    clock=Clock(),
                    values={"active_task_step_id": publish_step_id, "episode_id": episode_id},
                )
            )
        )
        assert publication_result.output_count == 1
        assert (tmp_path / "public" / "feed.xml").is_file()
        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            episode = EpisodeRepository(unit.session).get(episode_id)
            assert episode is not None
            assert episode.status is EpisodeStatus.PUBLISHED
            assert (
                publication_result.details["publication_status"]
                == PublicationStatus.PUBLISHED.value
            )
    finally:
        factory.kw["bind"].dispose()
