"""Configuration precedence and validation tests."""

from pathlib import Path

import pytest

from dailycast.core.config import load_settings
from dailycast.core.errors import ConfigurationError


def test_environment_overrides_yaml(app_config_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nested DailyCast environment variables take precedence over YAML values."""
    monkeypatch.setenv("DAILYCAST_APP__SERVER__PORT", "9012")

    settings = load_settings(config_path=app_config_path)

    assert settings.app.server.port == 9012
    assert settings.app.name == "DailyCast"


def test_dotenv_overrides_yaml_when_environment_is_absent(
    app_config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local .env file is loaded without hard-coding configuration values."""
    env_file = tmp_path / ".env"
    env_file.write_text("DAILYCAST_APP__SERVER__HOST=0.0.0.0\n", encoding="utf-8")
    monkeypatch.delenv("DAILYCAST_APP__SERVER__HOST", raising=False)

    settings = load_settings(config_path=app_config_path, env_file=env_file)

    assert settings.app.server.host == "0.0.0.0"


def test_processing_defaults_are_loaded_from_configuration(app_config_path: Path) -> None:
    """Processing bounds are explicit configuration rather than hidden rule constants."""
    settings = load_settings(config_path=app_config_path)

    assert settings.processing.max_age_hours == 36
    assert settings.processing.min_content_length == 300
    assert settings.processing.similarity_threshold == 0.58


def test_tts_and_ffmpeg_defaults_are_explicit_configuration(app_config_path: Path) -> None:
    """Draft audio roots and semantic provider settings are never hidden in service code."""
    settings = load_settings(config_path=app_config_path)

    assert settings.tts.provider == "edge_tts"
    assert settings.tts.voice == "zh-CN-XiaoxiaoNeural"
    assert settings.tts.cache_enabled is True
    assert settings.ffmpeg.sample_rate == 24_000
    assert settings.ffmpeg.bitrate == "64k"


def test_publishing_defaults_keep_review_gated_rss_explicit(app_config_path: Path) -> None:
    """RSS channel identity and origin do not default to automatic publication."""
    settings = load_settings(config_path=app_config_path)

    assert settings.publishing.auto_publish is False
    assert settings.publishing.public_base_url == "http://127.0.0.1:8000"
    assert settings.publishing.feed_title == "DailyCast"


def test_missing_yaml_fails_fast(tmp_path: Path) -> None:
    """The loader fails safely when the configured YAML file is unavailable."""
    with pytest.raises(ConfigurationError, match="does not exist"):
        load_settings(config_path=tmp_path / "missing.yaml")
