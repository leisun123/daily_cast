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


def test_tts_voice_dotenv_overrides_yaml_when_environment_is_absent(
    app_config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The local voice selection is visible and takes precedence over the YAML default."""
    env_file = tmp_path / ".env"
    env_file.write_text("DAILYCAST_TTS__VOICE=zh-CN-YunjianNeural\n", encoding="utf-8")
    monkeypatch.delenv("DAILYCAST_TTS__VOICE", raising=False)

    settings = load_settings(config_path=app_config_path, env_file=env_file)

    assert settings.tts.voice == "zh-CN-YunjianNeural"


def test_processing_defaults_are_loaded_from_configuration(app_config_path: Path) -> None:
    """Processing bounds are explicit configuration rather than hidden rule constants."""
    settings = load_settings(config_path=app_config_path)

    assert settings.processing.max_age_hours == 36
    assert settings.processing.source_max_age_hours == {
        "changzhou-public-recruitment": 336,
        "jiangsu-civil-service-notices": 336,
    }
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


def test_alpha_example_keeps_semantic_review_relaxed_and_auto_publish(tmp_path: Path) -> None:
    """The shipped Alpha configuration stays relaxed for semantic review and auto-publishes."""
    settings = load_settings(
        config_path=Path(__file__).resolve().parents[1] / "config" / "app.example.yaml",
        env_file=tmp_path / "absent.env",
    )

    assert settings.editorial.enforce_quality_gate is False
    assert settings.editorial.min_recruitment_events_when_available == 1
    assert settings.publishing.auto_publish is True


def test_publication_platforms_default_to_rss_only_and_keep_netease_profile_private(
    app_config_path: Path,
) -> None:
    """Distribution defaults preserve RSS while leaving browser automation explicitly disabled."""
    settings = load_settings(config_path=app_config_path)

    assert settings.publishing.rss.enabled is True
    assert settings.publishing.netease.enabled is False
    assert settings.publishing.netease.profile_dir == Path("netease/profile")
    assert settings.publishing.netease.creator_url == "https://music.163.com/creatorcenter"
    assert settings.publishing.netease.cover_path is None
    assert settings.publishing.xiaoyuzhou.enabled is False
    assert settings.publishing.public_base_url == "http://127.0.0.1:8000"
    assert settings.publishing.feed_title == "DailyCast"
    assert settings.llm.model == "gpt-5.6-terra"
    assert settings.tts.text_mode == "enhanced_text"
    assert settings.resolve_path(settings.tts.pronunciation_dictionary_path).is_file()


def test_external_distribution_requires_the_rss_source_of_truth() -> None:
    """NetEase and Xiaoyuzhou must not run without the immutable RSS asset publisher."""
    with pytest.raises(ValueError, match="requires publishing.rss.enabled=true"):
        PublishingSettings.model_validate(
            {
                "rss": {"enabled": False},
                "netease": {"enabled": True},
            }
        )


def test_alpha_example_uses_json_object_for_deepseek_fallback(tmp_path: Path) -> None:
    """The checked-in DeepSeek fallback must use the response mode its API accepts."""
    settings = load_settings(
        config_path=Path(__file__).resolve().parents[1] / "config" / "app.example.yaml",
        env_file=tmp_path / "absent.env",
    )

    assert settings.llm.fallback is not None
    assert settings.llm.fallback.response_format == "json_object"


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
    assert settings.llm.timeout_seconds == 120
    assert settings.llm.budget.max_input_tokens == 100_000
    # The briefing budget work reintroduced explicit per-provider and per-run
    # output ceilings so BudgetReservingLLMProvider can reserve per attempt.
    assert settings.llm.max_output_tokens == 2000
    assert settings.llm.budget.max_output_tokens == 15_000


def test_zeabur_eight_variable_interface_keeps_fallback_in_json_object_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Eight dynamic endpoint values combine with the stable DeepSeek response mode."""
    monkeypatch.setenv("DAILYCAST_LLM__PROVIDER", "openai_responses")
    monkeypatch.setenv("DAILYCAST_LLM__BASE_URL", "https://gateway.example/v1")
    monkeypatch.setenv("DAILYCAST_LLM__MODEL", "gpt-5.6-terra")
    monkeypatch.setenv("DAILYCAST_LLM__API_KEY", "primary-secret")
    monkeypatch.setenv("DAILYCAST_LLM__FALLBACK__PROVIDER", "openai_compatible")
    monkeypatch.setenv("DAILYCAST_LLM__FALLBACK__BASE_URL", "https://api.deepseek.example")
    monkeypatch.setenv("DAILYCAST_LLM__FALLBACK__MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("DAILYCAST_LLM__FALLBACK__API_KEY", "fallback-secret")
    monkeypatch.delenv("DAILYCAST_LLM__FALLBACK__RESPONSE_FORMAT", raising=False)

    settings = load_settings(
        config_path=Path(__file__).resolve().parents[1] / "config" / "zeabur.yaml",
        env_file=tmp_path / "absent.env",
    )

    assert settings.llm.fallback is not None
    assert settings.llm.fallback.response_format == "json_object"


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


def test_dailycast_llm_primary_and_fallback_environment_override_yaml(
    app_config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The explicit eight-variable interface configures both ordered endpoints."""
    monkeypatch.setenv("DAILYCAST_LLM__PROVIDER", "openai_responses")
    monkeypatch.setenv("DAILYCAST_LLM__BASE_URL", "https://gateway.example/v1")
    monkeypatch.setenv("DAILYCAST_LLM__MODEL", "gpt-5.6-terra")
    monkeypatch.setenv("DAILYCAST_LLM__API_KEY", "primary-secret")
    monkeypatch.setenv("DAILYCAST_LLM__FALLBACK__PROVIDER", "openai_compatible")
    monkeypatch.setenv("DAILYCAST_LLM__FALLBACK__BASE_URL", "https://api.deepseek.example")
    monkeypatch.setenv("DAILYCAST_LLM__FALLBACK__MODEL", "deepseek-test")
    monkeypatch.setenv("DAILYCAST_LLM__FALLBACK__API_KEY", "fallback-secret")

    settings = load_settings(config_path=app_config_path, env_file=tmp_path / "absent.env")

    assert settings.llm.provider == "openai_responses"
    assert settings.llm.base_url == "https://gateway.example/v1"
    assert settings.llm.api_key == "primary-secret"
    assert settings.llm.fallback is not None
    assert settings.llm.fallback.provider == "openai_compatible"
    assert settings.llm.fallback.base_url == "https://api.deepseek.example"
    assert settings.llm.fallback.model == "deepseek-test"
    assert settings.llm.fallback.api_key == "fallback-secret"


def test_llm_settings_load_primary_and_fallback_from_dotenv(
    app_config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local .env files use the same eight-variable interface as deployments."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                "DAILYCAST_LLM__PROVIDER=openai_responses",
                "DAILYCAST_LLM__BASE_URL=https://gateway.example/v1",
                "DAILYCAST_LLM__MODEL=gpt-5.6-terra",
                "DAILYCAST_LLM__API_KEY=primary-secret",
                "DAILYCAST_LLM__FALLBACK__PROVIDER=openai_compatible",
                "DAILYCAST_LLM__FALLBACK__BASE_URL=https://api.deepseek.example",
                "DAILYCAST_LLM__FALLBACK__MODEL=deepseek-test",
                "DAILYCAST_LLM__FALLBACK__API_KEY=fallback-secret",
                "",
            )
        ),
        encoding="utf-8",
    )
    for name in (
        "DAILYCAST_LLM__PROVIDER",
        "DAILYCAST_LLM__BASE_URL",
        "DAILYCAST_LLM__MODEL",
        "DAILYCAST_LLM__API_KEY",
        "DAILYCAST_LLM__FALLBACK__PROVIDER",
        "DAILYCAST_LLM__FALLBACK__BASE_URL",
        "DAILYCAST_LLM__FALLBACK__MODEL",
        "DAILYCAST_LLM__FALLBACK__API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = load_settings(config_path=app_config_path, env_file=env_file)

    assert settings.llm.provider == "openai_responses"
    assert settings.llm.base_url == "https://gateway.example/v1"
    assert settings.llm.model == "gpt-5.6-terra"
    assert settings.llm.api_key == "primary-secret"
    assert settings.llm.fallback is not None
    assert settings.llm.fallback.provider == "openai_compatible"
    assert settings.llm.fallback.base_url == "https://api.deepseek.example"
    assert settings.llm.fallback.model == "deepseek-test"
    assert settings.llm.fallback.api_key == "fallback-secret"


def test_legacy_llm_environment_values_do_not_override_dailycast_settings(
    app_config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unprefixed legacy process variables are outside the supported interface."""
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("LLM_BASE_URL", "https://legacy.example/v1")

    settings = load_settings(config_path=app_config_path, env_file=tmp_path / "absent.env")

    assert settings.llm.provider == "openai_responses"
    assert settings.llm.base_url == "https://api.openai.com/v1"


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
