"""Readiness endpoint checks for local Sprint 0 dependencies."""

from pathlib import Path

import pytest
from alembic import command
from fastapi.testclient import TestClient

from dailycast.core.config import load_settings
from dailycast.core.readiness import CheckResult
from dailycast.db.revision import build_alembic_config
from dailycast.main import create_app
from dailycast.pipeline.executor import InProcessTaskExecutor


def test_readyz_reports_revision_mismatch(app_config_path: Path) -> None:
    """A fresh SQLite file is not ready until the required Alembic revision exists."""
    with TestClient(create_app(config_path=app_config_path)) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    revision = next(check for check in body["checks"] if check["name"] == "alembic_revision")
    assert revision["ok"] is False


def test_readyz_succeeds_after_migration(
    app_config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Readiness succeeds once local checks, including the migration head, are valid."""
    settings = load_settings(config_path=app_config_path)
    config = build_alembic_config(
        ini_path=Path(__file__).resolve().parents[1] / "alembic.ini",
        database_url=settings.database.url,
    )
    command.upgrade(config, "head")

    from dailycast.core import readiness

    monkeypatch.setattr(
        readiness,
        "_check_ffmpeg",
        lambda: CheckResult(name="ffmpeg", ok=True, detail="test-double"),
    )

    with TestClient(create_app(config_path=app_config_path)) as client:
        response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_readyz_fails_closed_when_the_single_task_worker_is_unhealthy(
    app_config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live API without its sole supervised worker must not report ready."""
    settings = load_settings(config_path=app_config_path)
    config = build_alembic_config(
        ini_path=Path(__file__).resolve().parents[1] / "alembic.ini",
        database_url=settings.database.url,
    )
    command.upgrade(config, "head")

    from dailycast.core import readiness

    monkeypatch.setattr(
        readiness,
        "_check_ffmpeg",
        lambda: CheckResult(name="ffmpeg", ok=True, detail="test-double"),
    )
    monkeypatch.setattr(
        InProcessTaskExecutor,
        "is_healthy",
        property(lambda _executor: False),
    )

    with TestClient(create_app(config_path=app_config_path)) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    worker = next(check for check in response.json()["checks"] if check["name"] == "task_worker")
    assert worker["ok"] is False
