"""Minimal server-rendered DailyCast experience tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker
from test_publishing import _ready_episode, _service

from dailycast.core.config import load_settings
from dailycast.core.hashes import sha256_text
from dailycast.core.lifespan import build_daily_generation_command
from dailycast.core.time import Clock
from dailycast.db.models import EpisodeStatus, TaskRunStatus, TaskStepStatus, TaskType, TriggerType
from dailycast.db.repositories import TaskRunRepository, TaskStepRepository
from dailycast.db.transactions import UnitOfWork
from dailycast.episodes.service import EpisodeService
from dailycast.main import create_app
from dailycast.pipeline.context import PipelineContext
from dailycast.pipeline.contracts import canonical_json
from dailycast.pipeline.steps.publish import PublishStep
from dailycast.publishing.dispatcher import PublicationDispatcher


def _factory(app_config_path: Path) -> sessionmaker[Session]:
    """Build the real schema used by the lightweight web routes."""
    return __import__(
        "editorial_test_support", fromlist=["upgraded_session_factory"]
    ).upgraded_session_factory(app_config_path)


def _published_episode(factory: sessionmaker[Session], tmp_path: Path) -> object:
    """Create one durable published episode with its RSS public audio asset."""
    episode = _ready_episode(factory, tmp_path, key="web", day=22)
    _service(factory, tmp_path).publish(episode.id)
    return episode


def test_homepage_renders_latest_published_episode(app_config_path: Path, tmp_path: Path) -> None:
    """The daily landing page presents the newest episode and its public player."""
    factory = _factory(app_config_path)
    try:
        episode = _published_episode(factory, tmp_path)
        with TestClient(create_app(config_path=app_config_path)) as client:
            response = client.get("/")

        assert response.status_code == 200
        assert "今日科技新闻" in response.text
        assert 'action="/generate"' in response.text
        assert f"/episodes/{episode.id}" in response.text
        assert f"/media/episodes/{episode.public_id}/" in response.text
        assert 'href="/feed.xml"' in response.text
        assert "节目时长：0:00:03" in response.text
        assert "新闻话题：1 条" in response.text
    finally:
        factory.kw["bind"].dispose()


def test_episode_page_renders_public_audio_script_items_and_source_count(
    app_config_path: Path, tmp_path: Path
) -> None:
    """An episode detail page exposes only published audio plus frozen editorial context."""
    factory = _factory(app_config_path)
    try:
        episode = _published_episode(factory, tmp_path)
        with TestClient(create_app(config_path=app_config_path)) as client:
            response = client.get(f"/episodes/{episode.id}")

        assert response.status_code == 200
        assert "适合中文播报" in response.text
        assert "事件 web" in response.text
        assert "来源数：1" in response.text
        assert "来源与原文" in response.text
        assert "Source web" in response.text
        assert "事件 web" in response.text
        assert 'href="https://news.example.test/web"' in response.text
        assert "节目时长：0:00:03" in response.text
        assert "收录新闻：1 条" in response.text
        assert "生成时间：" in response.text
        assert f"/media/episodes/{episode.public_id}/" in response.text
    finally:
        factory.kw["bind"].dispose()


def test_latest_task_page_renders_run_steps_duration_and_error(app_config_path: Path) -> None:
    """The operator can inspect the newest execution without a JSON-only API call."""
    factory = _factory(app_config_path)
    try:
        now = datetime.now(UTC)
        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            task_run = TaskRunRepository(unit.session).create(
                id="web-task-run",
                task_type=TaskType.DAILY_GENERATE,
                business_key="daily_generate:2026-07-22",
                idempotency_key="web-task-run",
                trigger_type=TriggerType.MANUAL,
                status=TaskRunStatus.FAILED,
                pipeline_version="rss-v1",
                config_fingerprint="a" * 64,
                config_snapshot_json="{}",
                request_json="{}",
                started_at=now - timedelta(seconds=2),
                ended_at=now,
                error_code="EXTRACTION_FAILED",
                error_summary="网页提取失败",
            )
            TaskStepRepository(unit.session).create(
                task_run_id=task_run.id,
                step_name="extracting",
                step_order=2,
                attempt=1,
                status=TaskStepStatus.FAILED,
                started_at=now - timedelta(seconds=2),
                ended_at=now,
                details_json="{}",
                error_code="EXTRACTION_FAILED",
                error_summary="网页提取失败",
            )

        with TestClient(create_app(config_path=app_config_path)) as client:
            response = client.get("/tasks/latest")

        assert response.status_code == 200
        assert "web-task-run" in response.text
        assert "extracting" in response.text
        assert "网页提取失败" in response.text
        assert "2 秒" in response.text
    finally:
        factory.kw["bind"].dispose()


def test_generate_submits_durable_task_run_through_submission_service(
    app_config_path: Path,
) -> None:
    """Manual generation persists work through TaskSubmissionService instead of running inline."""
    factory = _factory(app_config_path)
    try:
        with TestClient(create_app(config_path=app_config_path)) as client:
            response = client.post("/generate")

        assert response.status_code == 202
        payload = response.json()
        assert payload["task_id"]
        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            assert TaskRunRepository(unit.session).get(payload["task_id"]) is not None
    finally:
        factory.kw["bind"].dispose()


def test_generate_retries_after_terminal_run_and_honors_explicit_idempotency_key(
    app_config_path: Path,
) -> None:
    """A prior terminal attempt does not prevent a new manual click from queuing a retry."""
    factory = _factory(app_config_path)
    try:
        settings = load_settings(config_path=app_config_path)
        command = build_daily_generation_command(settings, trigger_type=TriggerType.MANUAL)
        request_json = canonical_json(command.request)
        config_snapshot_json = canonical_json(command.config_snapshot)
        business_key = f"daily:{command.request['episode_date']}:daily:{command.pipeline_version}"
        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            TaskRunRepository(unit.session).create(
                id="previous-terminal-run",
                task_type=command.task_type,
                business_key=business_key,
                idempotency_key="previous-terminal-request",
                trigger_type=command.trigger_type,
                status=TaskRunStatus.FAILED,
                pipeline_version=command.pipeline_version,
                config_fingerprint=sha256_text(config_snapshot_json),
                config_snapshot_json=config_snapshot_json,
                request_json=request_json,
            )

        with TestClient(create_app(config_path=app_config_path)) as client:
            retry = client.post("/generate")
            first_explicit = client.post(
                "/generate", headers={"Idempotency-Key": "manual-web-retry-test"}
            )
            second_explicit = client.post(
                "/generate", headers={"Idempotency-Key": "manual-web-retry-test"}
            )

        assert retry.status_code == 202
        assert retry.json()["task_id"] != "previous-terminal-run"
        assert first_explicit.status_code == 202
        assert second_explicit.status_code == 202
        assert first_explicit.json()["task_id"] == second_explicit.json()["task_id"]
    finally:
        factory.kw["bind"].dispose()


def test_auto_publish_enabled_invokes_existing_publish_step(app_config_path: Path) -> None:
    """The configured auto-publish flag drives the existing publisher checkpoint."""

    class RecordingPublicationDispatcher:
        def __init__(self) -> None:
            self.published_episode_ids: list[int] = []

        async def publish(self, episode_id: int) -> SimpleNamespace:
            self.published_episode_ids.append(episode_id)
            return SimpleNamespace(
                rss_publication=SimpleNamespace(
                    id=7,
                    status=SimpleNamespace(value="published"),
                    public_asset_path="media/episodes/episode/asset.mp3",
                    feed_guid="episode",
                    response_summary_json='{"asset_reused":false,"feed_version":"v1"}',
                ),
                target_statuses={"rss": "published"},
                warning_count=0,
            )

    class RecordingEpisodeService:
        def __init__(self) -> None:
            self.approved_episode_ids: list[int] = []

        def get_episode(self, episode_id: int) -> SimpleNamespace:
            assert episode_id == 42
            return SimpleNamespace(status=EpisodeStatus.REVIEW_REQUIRED)

        def approve(self, episode_id: int) -> SimpleNamespace:
            self.approved_episode_ids.append(episode_id)
            return SimpleNamespace()

    factory = _factory(app_config_path)
    try:
        app_config_path.write_text(
            app_config_path.read_text(encoding="utf-8") + "\npublishing:\n  auto_publish: true\n",
            encoding="utf-8",
        )
        settings = load_settings(config_path=app_config_path)
        service = RecordingPublicationDispatcher()
        episode_service = RecordingEpisodeService()
        context = PipelineContext(
            task_run_id="web-auto-publish",
            session_factory=factory,
            shutdown_requested=asyncio.Event(),
            clock=Clock(),
            values={"episode_id": 42, "active_task_step_id": 1},
        )

        result = asyncio.run(
            PublishStep(
                cast(EpisodeService, episode_service),
                cast(PublicationDispatcher, service),
                auto_publish=settings.publishing.auto_publish,
            ).run(context)
        )

        assert settings.publishing.auto_publish is True
        assert episode_service.approved_episode_ids == [42]
        assert service.published_episode_ids == [42]
        assert result.output_count == 1
    finally:
        factory.kw["bind"].dispose()
