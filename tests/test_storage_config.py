"""Storage-path configuration coverage for Episode artifact persistence."""

from pathlib import Path

import pytest

from dailycast.core.config import load_settings


def test_data_and_public_dir_environment_variables_override_yaml(
    app_config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The documented direct DATA_DIR and PUBLIC_DIR variables control all runtime roots."""
    data_dir = tmp_path / "persistent-data"
    public_dir = tmp_path / "persistent-public"
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("PUBLIC_DIR", str(public_dir))

    settings = load_settings(config_path=app_config_path)

    assert settings.data_dir == data_dir
    assert settings.public_dir == public_dir
