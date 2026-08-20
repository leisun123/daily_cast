"""Briefing lifespan wiring and HTTP endpoint tests with unreachable fake sources."""

from __future__ import annotations

import asyncio
from pathlib import Path

from editorial_test_support import upgraded_session_factory
from fastapi.testclient import TestClient

from dailycast.main import create_app


def _enable_briefing(app_config_path: Path, tmp_path: Path) -> None:
    """Point the briefing flow at one tagged source that can never be collected."""
    sources_path = tmp_path / "briefing.sources.yaml"
    sources_path.write_text(
        "sources:\n"
        "  - id: briefing-test-source\n"
        "    name: 测试来源\n"
        "    kind: rss\n"
        "    entry_url: https://briefing-test.invalid/rss\n"
        "    config:\n"
        "      briefing_category: telecom\n",
        encoding="utf-8",
    )
    app_config_path.write_text(
        app_config_path.read_text(encoding="utf-8")
        + "briefing:\n"
        + "  enabled: true\n"
        + f"  sources_config_path: {sources_path}\n",
        encoding="utf-8",
    )


def test_briefing_endpoints_are_wired_when_enabled(app_config_path: Path, tmp_path: Path) -> None:
    """An enabled deployment exposes the manual trigger and the latest-briefing read."""
    _enable_briefing(app_config_path, tmp_path)
    upgraded_session_factory(app_config_path)

    with TestClient(create_app(config_path=app_config_path)) as client:
        generate_response = client.post("/briefing/generate")
        latest_response = client.get("/briefing/latest")
        runtime = client.app.state.runtime

    assert generate_response.status_code == 202
    assert generate_response.json() == {"status": "accepted"}
    # The only configured source is unreachable, so nothing can have been generated yet.
    assert latest_response.status_code == 404
    assert runtime.briefing_service is not None


def test_briefing_generate_conflicts_when_disabled(app_config_path: Path) -> None:
    """A deployment without the briefing flow rejects manual triggers explicitly."""
    upgraded_session_factory(app_config_path)

    with TestClient(create_app(config_path=app_config_path)) as client:
        generate_response = client.post("/briefing/generate")
        latest_response = client.get("/briefing/latest")

    assert generate_response.status_code == 409
    assert latest_response.status_code == 404


def test_briefing_generate_returns_409_while_a_run_is_in_progress(
    app_config_path: Path, tmp_path: Path
) -> None:
    """A second manual trigger overlaps an active run with a clear conflict response."""
    _enable_briefing(app_config_path, tmp_path)
    upgraded_session_factory(app_config_path)

    with TestClient(create_app(config_path=app_config_path)) as client:
        runtime = client.app.state.runtime
        assert runtime.briefing_service is not None
        # Hold the run lock from the test side to make the overlap deterministic.
        lock = runtime.briefing_service._run_lock
        asyncio.run(lock.acquire())
        try:
            response = client.post("/briefing/generate")
        finally:
            lock.release()

    assert response.status_code == 409
    assert response.json() == {"detail": "briefing run already in progress"}


def test_briefing_generate_accepts_the_force_parameter(
    app_config_path: Path, tmp_path: Path
) -> None:
    """The force query parameter is accepted and still schedules a background run."""
    _enable_briefing(app_config_path, tmp_path)
    upgraded_session_factory(app_config_path)

    with TestClient(create_app(config_path=app_config_path)) as client:
        response = client.post("/briefing/generate?force=true")

    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
