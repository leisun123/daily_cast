"""Briefing lifespan wiring and HTTP endpoint tests with unreachable fake sources."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from editorial_test_support import upgraded_session_factory
from fastapi.testclient import TestClient

from dailycast.db.models import SourceKind
from dailycast.db.repositories import SourceRepository
from dailycast.db.transactions import UnitOfWork
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


def test_briefing_runtime_seeds_the_two_management_web_research_sources(
    app_config_path: Path,
) -> None:
    """Native web discovery is available only after the briefing runtime has been enabled."""
    app_config_path.write_text(
        app_config_path.read_text(encoding="utf-8") + "briefing:\n  enabled: true\n",
        encoding="utf-8",
    )
    upgraded_session_factory(app_config_path)

    with TestClient(create_app(config_path=app_config_path)) as client:
        runtime = client.app.state.runtime
        with UnitOfWork(runtime.session_factory) as unit:
            assert unit.session is not None
            sources = SourceRepository(unit.session)
            telecom = sources.get("openai-web-research-telecom-management")
            ai = sources.get("openai-web-research-ai-management")

    assert telecom is not None and telecom.kind is SourceKind.WEB_RESEARCH
    assert ai is not None and ai.kind is SourceKind.WEB_RESEARCH


def test_briefing_runtime_refreshes_an_existing_source_from_current_configuration(
    app_config_path: Path, tmp_path: Path
) -> None:
    """A redeploy applies current briefing YAML instead of retaining stale database options."""
    _enable_briefing(app_config_path, tmp_path)
    factory = upgraded_session_factory(app_config_path)
    with UnitOfWork(factory) as unit:
        assert unit.session is not None
        SourceRepository(unit.session).create(
            id="briefing-test-source",
            name="旧测试来源",
            kind=SourceKind.RSS,
            entry_url="https://briefing-test.invalid/rss",
            normalized_entry_url="https://briefing-test.invalid/rss",
            enabled=False,
            priority=1,
            language="en",
            config_json=json.dumps({"briefing_category": "ai", "query": "已经废弃的线上查询"}),
            request_timeout_seconds=99,
            max_items_per_run=1,
        )

    with TestClient(create_app(config_path=app_config_path)) as client:
        runtime = client.app.state.runtime
        with UnitOfWork(runtime.session_factory) as unit:
            assert unit.session is not None
            source = SourceRepository(unit.session).get("briefing-test-source")
            assert source is not None
            persisted = {
                "name": source.name,
                "enabled": source.enabled,
                "priority": source.priority,
                "language": source.language,
                "config": json.loads(source.config_json),
                "request_timeout_seconds": source.request_timeout_seconds,
                "max_items_per_run": source.max_items_per_run,
            }

    assert persisted == {
        "name": "测试来源",
        "enabled": True,
        "priority": 50,
        "language": None,
        "config": {"briefing_category": "telecom"},
        "request_timeout_seconds": 20,
        "max_items_per_run": 50,
    }


def test_briefing_generate_conflicts_when_disabled(app_config_path: Path) -> None:
    """A deployment without the briefing flow rejects manual triggers explicitly."""
    upgraded_session_factory(app_config_path)

    with TestClient(create_app(config_path=app_config_path)) as client:
        generate_response = client.post("/briefing/generate")
        push_response = client.post("/briefing/test-push")
        latest_response = client.get("/briefing/latest")

    assert generate_response.status_code == 409
    assert push_response.status_code == 409
    assert push_response.json() == {"detail": "briefing is not enabled"}
    assert latest_response.status_code == 404


def test_briefing_test_push_conflicts_when_webhook_is_disabled(
    app_config_path: Path, tmp_path: Path
) -> None:
    """The manual push trigger names the missing webhook instead of sending nothing."""
    _enable_briefing(app_config_path, tmp_path)
    upgraded_session_factory(app_config_path)

    with TestClient(create_app(config_path=app_config_path)) as client:
        response = client.post("/briefing/test-push")

    assert response.status_code == 409
    assert response.json() == {"detail": "briefing webhook is not enabled"}


def test_briefing_test_push_reports_webhook_delivery_failures(
    app_config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreachable webhook turns into a 502 carrying the push error for debugging."""
    _enable_briefing(app_config_path, tmp_path)
    app_config_path.write_text(
        app_config_path.read_text(encoding="utf-8") + "  webhook_enabled: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DAILYCAST_BRIEFING__WEBHOOK_URL", "https://briefing-push.invalid/hook")
    upgraded_session_factory(app_config_path)

    with TestClient(create_app(config_path=app_config_path)) as client:
        response = client.post("/briefing/test-push")

    assert response.status_code == 502
    assert response.json()["detail"].startswith("webhook push failed")


def test_briefing_generate_returns_409_while_a_run_is_in_progress(
    app_config_path: Path, tmp_path: Path
) -> None:
    """A second manual trigger overlaps an active run with a clear conflict response."""
    _enable_briefing(app_config_path, tmp_path)
    upgraded_session_factory(app_config_path)

    with TestClient(create_app(config_path=app_config_path)) as client:
        runtime = client.app.state.runtime
        assert runtime.briefing_service is not None
        # Reserve the single run slot from the test side to make the overlap deterministic.
        service = runtime.briefing_service
        service._try_reserve_run()
        try:
            response = client.post("/briefing/generate")
        finally:
            service._run_reserved = False

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


def test_briefing_test_push_is_bearer_protected_on_public_deployments(
    app_config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On public_only deployments the manual push trigger requires the operator token."""
    token = "test-trigger-token-0123456789abcdef0123456789abcdef"
    _enable_briefing(app_config_path, tmp_path)
    app_config_path.write_text(
        app_config_path.read_text(encoding="utf-8").replace(
            "  environment: test\n",
            f"  environment: test\n  public_only: true\n  manual_trigger_token: {token}\n",
        )
        + "  webhook_enabled: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DAILYCAST_BRIEFING__WEBHOOK_URL", "https://briefing-push.invalid/hook")
    upgraded_session_factory(app_config_path)

    with TestClient(create_app(config_path=app_config_path)) as client:
        unauthenticated = client.post("/briefing/test-push")
        wrong_token = client.post(
            "/briefing/test-push", headers={"Authorization": "Bearer wrong-token"}
        )
        accepted = client.post("/briefing/test-push", headers={"Authorization": f"Bearer {token}"})
        generate_unauthenticated = client.post("/briefing/generate")
        generate_accepted = client.post(
            "/briefing/generate", headers={"Authorization": f"Bearer {token}"}
        )
        latest_accepted = client.get(
            "/briefing/latest", headers={"Authorization": f"Bearer {token}"}
        )

    assert unauthenticated.status_code == 401
    assert wrong_token.status_code == 401
    # The valid token reaches the handler; the unreachable webhook surfaces as 502.
    assert accepted.status_code == 502
    assert accepted.json()["detail"].startswith("webhook push failed")
    # The other briefing routes share the same public-deployment gate.
    assert generate_unauthenticated.status_code == 401
    assert generate_accepted.status_code == 202
    assert latest_accepted.status_code == 404
