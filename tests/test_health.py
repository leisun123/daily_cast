"""Liveness endpoint tests."""

from pathlib import Path

import yaml
from editorial_test_support import upgraded_session_factory
from fastapi.testclient import TestClient

from dailycast.main import create_app


def test_root_and_healthz_are_available(app_config_path: Path) -> None:
    """The minimal web home and process liveness endpoint are available."""
    factory = upgraded_session_factory(app_config_path)
    try:
        with TestClient(create_app(config_path=app_config_path)) as client:
            root = client.get("/")
            health = client.get("/healthz", headers={"X-Request-ID": "test-request"})
    finally:
        factory.kw["bind"].dispose()

    assert root.status_code == 200
    assert "DailyCast" in root.text
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert health.headers["X-Request-ID"] == "test-request"


def test_public_only_mode_hides_management_routes(app_config_path: Path) -> None:
    """A public RSS deployment must not expose unauthenticated operator surfaces."""
    config = yaml.safe_load(app_config_path.read_text(encoding="utf-8"))
    config["app"]["public_only"] = True
    app_config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    factory = upgraded_session_factory(app_config_path)
    try:
        with TestClient(create_app(config_path=app_config_path)) as client:
            health = client.get("/healthz")
            ready = client.get("/readyz")
            root = client.get("/")
            task = client.get("/tasks/latest")
            generate = client.post("/generate")
            manual_trigger = client.post("/api/v1/manual/generate")
    finally:
        factory.kw["bind"].dispose()

    assert health.status_code == 200
    assert ready.status_code in {200, 503}
    assert ready.status_code != 404
    assert root.status_code == 404
    assert task.status_code == 404
    assert generate.status_code == 404
    assert manual_trigger.status_code == 404


def test_public_only_manual_trigger_requires_configured_bearer_token(
    app_config_path: Path, monkeypatch
) -> None:
    """A public deployment exposes generation only through the configured bearer token."""
    config = yaml.safe_load(app_config_path.read_text(encoding="utf-8"))
    config["app"]["public_only"] = True
    app_config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    token = "test-public-manual-trigger-token-123456"
    monkeypatch.setenv("DAILYCAST_APP__MANUAL_TRIGGER_TOKEN", token)
    factory = upgraded_session_factory(app_config_path)
    try:
        with TestClient(create_app(config_path=app_config_path)) as client:
            missing = client.post("/api/v1/manual/generate")
            invalid = client.post(
                "/api/v1/manual/generate",
                headers={"Authorization": "Bearer not-the-configured-token"},
            )
            accepted = client.post(
                "/api/v1/manual/generate",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Idempotency-Key": "public-manual-trigger-test",
                },
            )
    finally:
        factory.kw["bind"].dispose()

    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert accepted.status_code == 202


def test_public_only_briefing_dry_run_requires_configured_bearer_token(
    app_config_path: Path, monkeypatch
) -> None:
    """The dry-run route stays reachable on public deployments but stays token-guarded."""
    config = yaml.safe_load(app_config_path.read_text(encoding="utf-8"))
    config["app"]["public_only"] = True
    app_config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    token = "test-public-manual-trigger-token-123456"
    monkeypatch.setenv("DAILYCAST_APP__MANUAL_TRIGGER_TOKEN", token)
    factory = upgraded_session_factory(app_config_path)
    try:
        with TestClient(create_app(config_path=app_config_path)) as client:
            missing = client.post("/briefing/dry-run")
            invalid = client.post(
                "/briefing/dry-run",
                headers={"Authorization": "Bearer not-the-configured-token"},
            )
            # Briefing is disabled in this minimal config, so a valid token
            # proves the route was reached and answered from the handler.
            valid = client.post(
                "/briefing/dry-run", headers={"Authorization": f"Bearer {token}"}
            )
    finally:
        factory.kw["bind"].dispose()

    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert valid.status_code == 409
    assert valid.json() == {"detail": "briefing is not enabled"}
