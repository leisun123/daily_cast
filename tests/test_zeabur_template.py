"""Zeabur GitHub deployment contract tests."""

from pathlib import Path

import pytest
import yaml

from dailycast.core.config import load_settings


def test_zeabur_template_uses_github_and_persistent_runtime_volumes() -> None:
    """The public deployment must build main from GitHub and retain every durable artifact."""
    template_path = Path(__file__).parents[1] / "zeabur.yaml"
    resource = yaml.safe_load(template_path.read_text(encoding="utf-8"))
    service = resource["spec"]["services"][0]
    spec = service["spec"]

    assert service["template"] == "GIT"
    assert spec["source"] == {
        "source": "GITHUB",
        "repo": 1309505877,
        "branch": "main",
    }
    assert spec["volumes"] == [
        {"id": "data", "dir": "/app/data"},
        {"id": "public", "dir": "/app/public"},
    ]
    assert spec["healthCheck"] == {
        "type": "HTTP",
        "port": "web",
        "http": {"path": "/healthz"},
    }
    assert resource["spec"]["variables"] == [
        {
            "key": "PUBLIC_DOMAIN",
            "type": "DOMAIN",
            "name": "Public podcast domain",
            "description": "Public HTTPS domain used by the RSS Feed and immutable MP3 URLs.",
        },
        {
            "key": "DAILYCAST_LLM__BASE_URL",
            "type": "STRING",
            "name": "LLM base URL",
            "description": "OpenAI Responses-compatible endpoint without embedded credentials.",
        },
        {
            "key": "DAILYCAST_LLM__API_KEY",
            "type": "PASSWORD",
            "name": "LLM API key",
            "description": "Secret API key stored only in Zeabur environment configuration.",
        },
    ]
    assert spec["env"] == {
        "DAILYCAST_CONFIG_PATH": {
            "default": "/app/config/zeabur.yaml",
            "readonly": True,
        },
        "DAILYCAST_PUBLISHING__PUBLIC_BASE_URL": {
            "default": "${ZEABUR_WEB_URL}",
            "readonly": True,
        },
    }

    assert "configs" not in spec
    parsed_config = yaml.safe_load(
        (Path(__file__).parents[1] / "config" / "zeabur.yaml").read_text(encoding="utf-8")
    )
    assert parsed_config["app"]["public_only"] is True
    assert parsed_config["scheduler"] == {
        "enabled": True,
        "cron_expression": "0 6 * * *",
    }
    assert parsed_config["llm"] == {
        "provider": "openai_responses",
        "model": "gpt-5.6-terra",
    }
    assert parsed_config["publishing"]["auto_publish"] is True
    assert parsed_config["publishing"]["rss"]["enabled"] is True
    assert parsed_config["publishing"]["netease"]["enabled"] is True


def test_env_example_contains_only_values_the_operator_must_maintain() -> None:
    """Optional runtime tuning belongs in YAML rather than a duplicated environment list."""
    project_root = Path(__file__).parents[1]
    env_lines = {
        line.split("=", 1)[0]
        for line in (project_root / ".env.example").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    }

    assert env_lines == {
        "DAILYCAST_CONFIG_PATH",
        "DAILYCAST_LLM__BASE_URL",
        "DAILYCAST_LLM__API_KEY",
    }


def test_generated_zeabur_config_combines_three_inputs_with_yaml_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The compact deployment contract must still construct complete runtime settings."""
    project_root = Path(__file__).parents[1]
    config_template = (project_root / "config" / "zeabur.yaml").read_text(encoding="utf-8")
    config_path = tmp_path / "zeabur.yaml"
    config_path.write_text(config_template, encoding="utf-8")
    monkeypatch.setenv("DAILYCAST_LLM__BASE_URL", "https://gateway.example/v1")
    monkeypatch.setenv("DAILYCAST_LLM__API_KEY", "test-secret")
    monkeypatch.setenv("DAILYCAST_PUBLISHING__PUBLIC_BASE_URL", "https://dailycast.example")

    settings = load_settings(config_path=config_path, env_file=tmp_path / "missing.env")

    assert settings.app.public_only is True
    assert settings.scheduler.enabled is True
    assert settings.llm.provider == "openai_responses"
    assert settings.llm.model == "gpt-5.6-terra"
    assert settings.llm.base_url == "https://gateway.example/v1"
    assert settings.llm.api_key == "test-secret"
    assert settings.publishing.public_base_url == "https://dailycast.example"
    assert settings.publishing.netease.enabled is True
    assert settings.storage.data_dir == Path("data")
