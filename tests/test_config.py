"""Configuration precedence and validation tests."""

from pathlib import Path

import pytest
import yaml

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


def test_quality_gate_and_auto_publish_remain_strict_by_model_default() -> None:
    """Deployments must opt in to Alpha relaxation rather than inherit it silently."""
    assert EditorialSettings().enforce_quality_gate is True
    assert PublishingSettings().auto_publish is False


def test_default_llm_model_is_gpt_5_6_terra() -> None:
    """New installations use the production model verified against the Responses endpoint."""
    assert LLMSettings().model == "gpt-5.6-terra"


def test_distribution_targets_are_explicit_and_external_publishers_default_off() -> None:
    """RSS stays enabled while credential-bearing browser automation is opt-in."""
    settings = PublishingSettings()

    assert settings.rss.enabled is True
    assert settings.netease.enabled is False
    assert settings.netease.profile_dir == Path("netease/profile")
    assert settings.netease.storage_state_path == Path("netease/storage-state.json")
    assert settings.netease.headless is True
    assert settings.netease.creator_url == "https://musicupload.netease.com/"
    assert settings.xiaoyuzhou.enabled is False


@pytest.mark.parametrize("platform", ["netease", "xiaoyuzhou"])
def test_external_distribution_requires_rss_immutable_asset(platform: str) -> None:
    """External targets cannot run without the RSS-owned immutable MP3 source."""
    publishing = {
        "rss": {"enabled": False},
        platform: {"enabled": True},
    }

    with pytest.raises(
        ValueError,
        match=f"publishing.{platform}.enabled requires publishing.rss.enabled",
    ):
        PublishingSettings.model_validate(publishing)


def test_example_yaml_documents_all_distribution_targets() -> None:
    """Operators can enable NetEase without inventing undocumented nested keys."""
    example_path = Path(__file__).resolve().parents[1] / "config" / "app.example.yaml"
    raw = yaml.safe_load(example_path.read_text(encoding="utf-8"))

    assert raw["publishing"]["rss"] == {"enabled": True}
    assert raw["publishing"]["netease"]["enabled"] is False
    assert raw["publishing"]["netease"]["profile_dir"] == "netease/profile"
    assert raw["publishing"]["netease"]["storage_state_path"] == "netease/storage-state.json"
    assert raw["publishing"]["netease"]["headless"] is True
    assert raw["publishing"]["xiaoyuzhou"]["enabled"] is False


def test_application_timezone_must_be_an_iana_timezone() -> None:
    """A misspelled timezone must fail configuration rather than silently schedule at host time."""
    with pytest.raises(ValueError, match="app.timezone must be a valid IANA timezone"):
        AppSettings(timezone="Mars/Olympus")


def test_missing_yaml_fails_fast(tmp_path: Path) -> None:
    """The loader fails safely when the configured YAML file is unavailable."""
    with pytest.raises(ConfigurationError, match="does not exist"):
        load_settings(config_path=tmp_path / "missing.yaml")
