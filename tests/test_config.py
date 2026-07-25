"""Configuration precedence and validation tests."""

from pathlib import Path

import pytest

from dailycast.core.config import (
    AppSettings,
    EditorialSettings,
    LLMSettings,
    PublishingSettings,
    load_settings,
)
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
    assert settings.tts.text_mode == "enhanced_text"
    assert settings.tts.pronunciation_dictionary_path == Path("config/pronunciation.yaml")
    assert settings.tts.cache_enabled is True
    assert settings.tts.opening_summary_speed == 0.94
    assert settings.tts.closing_summary_speed == 0.94
    assert settings.ffmpeg.sample_rate == 24_000
    assert settings.ffmpeg.bitrate == "64k"


def test_alpha_example_unblocks_quality_gated_output_and_auto_publish(tmp_path: Path) -> None:
    """The shipped Alpha configuration records quality findings without blocking output."""
    settings = load_settings(
        config_path=Path(__file__).resolve().parents[1] / "config" / "app.example.yaml",
        env_file=tmp_path / "absent.env",
    )

    assert settings.editorial.enforce_quality_gate is False
    assert settings.publishing.auto_publish is True
    assert settings.publishing.public_base_url == "http://127.0.0.1:8000"
    assert settings.publishing.feed_title == "DailyCast"
    assert settings.llm.model == "gpt-5.6-terra"
    assert settings.tts.text_mode == "enhanced_text"
    assert settings.resolve_path(settings.tts.pronunciation_dictionary_path).is_file()


def test_zeabur_runtime_config_keeps_fixed_production_settings_out_of_environment(
    tmp_path: Path,
) -> None:
    """Zeabur supplies only dynamic values while this checked-in YAML owns stable defaults."""
    settings = load_settings(
        config_path=Path(__file__).resolve().parents[1] / "config" / "zeabur.yaml",
        env_file=tmp_path / "absent.env",
    )

    assert settings.app.environment == "production"
    assert settings.app.public_only is True
    assert settings.app.server.host == "0.0.0.0"
    assert settings.database.url == "sqlite:////app/data/dailycast.db"
    assert settings.data_dir == Path("/app/data")
    assert settings.public_dir == Path("/app/public")
    assert settings.scheduler.enabled is True
    assert settings.scheduler.cron_expression == "0 6 * * *"
    assert settings.editorial.enforce_quality_gate is False
    assert settings.publishing.auto_publish is True
    assert settings.llm.response_format == "json_object"


def test_zeabur_uses_production_config_when_an_existing_service_has_the_old_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deployed pre-template service upgrades to the checked-in Zeabur defaults."""
    monkeypatch.setenv("ZEABUR_WEB_URL", "https://dailycast.example")
    monkeypatch.setenv("DAILYCAST_CONFIG_PATH", "/app/config/app.example.yaml")

    settings = load_settings(env_file=tmp_path / "absent.env")

    assert settings.app.environment == "production"
    assert settings.app.public_only is True
    assert settings.scheduler.enabled is True


def test_llm_settings_use_canonical_llm_names_when_dailycast_values_are_placeholders(
    app_config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The canonical LLM variables keep a service working through the broken old rows."""
    monkeypatch.setenv("DAILYCAST_LLM__PROVIDER", "${LLM_PROVIDER}")
    monkeypatch.setenv("DAILYCAST_LLM__BASE_URL", "${LLM_BASE_URL}")
    monkeypatch.setenv("DAILYCAST_LLM__API_KEY", "${LLM_API_KEY}")
    monkeypatch.setenv("LLM_PROVIDER", "openai_responses")
    monkeypatch.setenv("LLM_BASE_URL", "https://gateway.example/v1")
    monkeypatch.setenv("LLM_API_KEY", "existing-secret")

    settings = load_settings(config_path=app_config_path, env_file=tmp_path / "absent.env")

    assert settings.llm.provider == "openai_responses"
    assert settings.llm.base_url == "https://gateway.example/v1"
    assert settings.llm.api_key == "existing-secret"


def test_dailycast_llm_values_are_ignored_in_favor_of_canonical_llm_names(
    app_config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only LLM_* is a supported environment interface for model configuration."""
    monkeypatch.setenv("DAILYCAST_LLM__PROVIDER", "openai_compatible")
    monkeypatch.setenv("DAILYCAST_LLM__BASE_URL", "https://native.example/v1")
    monkeypatch.setenv("LLM_PROVIDER", "openai_responses")
    monkeypatch.setenv("LLM_BASE_URL", "https://legacy.example/v1")

    settings = load_settings(config_path=app_config_path, env_file=tmp_path / "absent.env")

    assert settings.llm.provider == "openai_responses"
    assert settings.llm.base_url == "https://legacy.example/v1"


def test_quality_gate_and_auto_publish_remain_strict_by_model_default() -> None:
    """Deployments must opt in to Alpha relaxation rather than inherit it silently."""
    assert EditorialSettings().enforce_quality_gate is True
    assert PublishingSettings().auto_publish is False


def test_default_llm_model_is_gpt_5_6_terra() -> None:
    """New installations use the production model verified against the Responses endpoint."""
    assert LLMSettings().model == "gpt-5.6-terra"


def test_application_timezone_must_be_an_iana_timezone() -> None:
    """A misspelled timezone must fail configuration rather than silently schedule at host time."""
    with pytest.raises(ValueError, match="app.timezone must be a valid IANA timezone"):
        AppSettings(timezone="Mars/Olympus")


def test_missing_yaml_fails_fast(tmp_path: Path) -> None:
    """The loader fails safely when the configured YAML file is unavailable."""
    with pytest.raises(ConfigurationError, match="does not exist"):
        load_settings(config_path=tmp_path / "missing.yaml")
