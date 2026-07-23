"""Shared fixtures for Sprint 0 infrastructure tests."""

from pathlib import Path

import pytest


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
