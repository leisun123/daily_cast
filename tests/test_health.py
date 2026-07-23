"""Liveness endpoint tests."""

from pathlib import Path

from fastapi.testclient import TestClient

from dailycast.main import create_app


def test_root_and_healthz_are_available(app_config_path: Path) -> None:
    """Sprint 0 exposes only service identity and liveness before readiness."""
    with TestClient(create_app(config_path=app_config_path)) as client:
        root = client.get("/")
        health = client.get("/healthz", headers={"X-Request-ID": "test-request"})

    assert root.status_code == 200
    assert root.json() == {"message": "DailyCast API"}
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert health.headers["X-Request-ID"] == "test-request"
