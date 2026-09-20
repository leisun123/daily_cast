"""Shared fixtures for Sprint 0 infrastructure tests."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep host ALL_PROXY/HTTP(S)_PROXY from breaking httpx tests without socksio."""
    for name in (
        "ALL_PROXY",
        "all_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def app_config_path(tmp_path: Path) -> Path:
    """Create a valid isolated YAML config with writable runtime directories."""
    config_path = tmp_path / "app.yaml"
    data_dir = tmp_path / "data"
    public_dir = tmp_path / "public"
    config_path.write_text(
        "\n".join(
            [
                "app:",
                "  name: DailyCast",
                "  environment: test",
                "  timezone: Asia/Shanghai",
                "  server:",
                "    host: 127.0.0.1",
                "    port: 8000",
                "database:",
                f"  url: sqlite:///{tmp_path / 'dailycast.db'}",
                "  echo: false",
                "storage:",
                f"  data_dir: {data_dir}",
                f"  public_dir: {public_dir}",
                "logging:",
                "  level: INFO",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return config_path
