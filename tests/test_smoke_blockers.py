"""Regression tests for blockers discovered by the first Alpha smoke test."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import yaml
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from dailycast.core.time import Clock
from dailycast.db.models import TaskRunStatus, TaskStepStatus, TaskType, TriggerType
from dailycast.db.repositories import SourceRepository, TaskRunRepository, TaskStepRepository
from dailycast.db.transactions import UnitOfWork
from dailycast.main import create_app
from dailycast.pipeline.context import PipelineContext
from dailycast.pipeline.contracts import StepResult
from dailycast.pipeline.orchestrator import PipelineOrchestrator
from dailycast.pipeline.steps.ranking import RankingStep


def _factory(app_config_path: Path) -> sessionmaker[Session]:
    """Create an Alembic-upgraded isolated database for startup behavior checks."""
    return __import__(
        "editorial_test_support", fromlist=["upgraded_session_factory"]
    ).upgraded_session_factory(app_config_path)


def test_startup_seeds_missing_sources_from_default_source_configuration(
    app_config_path: Path,
) -> None:
    """A first Docker startup makes the configured Hacker News RSS source collectable."""
    factory = _factory(app_config_path)
    try:
        with TestClient(create_app(config_path=app_config_path)):
            pass

        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            sources = SourceRepository(unit.session).list()

        assert [(source.id, source.name) for source in sources] == [
            ("hacker-news-rss", "Hacker News")
        ]
    finally:
        factory.kw["bind"].dispose()


def test_compose_loads_optional_local_dotenv_inside_application_container() -> None:
    """The documented .env values, including API credentials, reach the application process."""
    compose_path = Path(__file__).resolve().parents[1] / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    env_files = compose["services"]["dailycast"].get("env_file", [])

    assert {entry["path"]: entry.get("required") for entry in env_files} == {".env": False}


class _NoEditorialCall:
    """Fail if an empty event set ever attempts an LLM call."""

    async def score_events(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("an empty event set must not call the editorial provider")


class _MustNotRun:
    """Catch an orchestrator that incorrectly advances past a terminal empty-result step."""

    name = "must_not_run"

    async def run(self, context: PipelineContext) -> StepResult:
        del context
        raise AssertionError("the pipeline must stop after no publishable events")


def test_empty_cluster_result_finishes_with_warning_without_editorial_call(
    app_config_path: Path,
) -> None:
    """No collected events is a normal no-episode outcome, not a failed outline validation."""

    async def scenario() -> None:
        factory = _factory(app_config_path)
        try:
            task_run_id = "empty-cluster-run"
            now = datetime.now(UTC)
            with UnitOfWork(factory) as unit:
                assert unit.session is not None
                TaskRunRepository(unit.session).create(
                    id=task_run_id,
                    task_type=TaskType.DAILY_GENERATE,
                    business_key="daily:empty-cluster",
                    idempotency_key="empty-cluster-run",
                    trigger_type=TriggerType.MANUAL,
                    status=TaskRunStatus.QUEUED,
                    pipeline_version="test-v1",
                    config_fingerprint="a" * 64,
                    config_snapshot_json="{}",
                    request_json="{}",
                    created_at=now,
                    updated_at=now,
                )

            orchestrator = PipelineOrchestrator(
                factory,
                (
                    RankingStep(_NoEditorialCall(), lambda: object()),  # type: ignore[arg-type]
                    _MustNotRun(),
                ),
                clock=Clock(),
            )
            await orchestrator.execute(task_run_id)

            with UnitOfWork(factory) as unit:
                assert unit.session is not None
                task_run = TaskRunRepository(unit.session).get(task_run_id)
                assert task_run is not None
                steps = TaskStepRepository(unit.session).list_by_task_run(task_run_id)

            assert task_run.status is TaskRunStatus.SUCCEEDED_WITH_WARNINGS
            assert task_run.warning_count == 1
            assert [step.step_name for step in steps] == ["ranking"]
            assert steps[0].status is TaskStepStatus.SUCCEEDED_WITH_WARNINGS
            assert steps[0].error_summary is None
        finally:
            factory.kw["bind"].dispose()

    asyncio.run(scenario())
