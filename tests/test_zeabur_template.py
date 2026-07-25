"""Zeabur GitHub deployment contract tests."""

from pathlib import Path

import yaml


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
    assert spec["env"]["DAILYCAST_CONFIG_PATH"]["default"] == "/app/config/zeabur.yaml"
    assert spec["env"]["DAILYCAST_PUBLISHING__PUBLIC_BASE_URL"]["default"] == "${ZEABUR_WEB_URL}"


def test_zeabur_template_exposes_only_the_canonical_llm_environment_names() -> None:
    """Template input names must match the only LLM variables application code reads."""
    template_path = Path(__file__).parents[1] / "zeabur.yaml"
    resource = yaml.safe_load(template_path.read_text(encoding="utf-8"))
    variable_keys = {variable["key"] for variable in resource["spec"]["variables"]}
    service_environment = resource["spec"]["services"][0]["spec"]["env"]

    assert {
        "LLM_PROVIDER",
        "LLM_BASE_URL",
        "LLM_MODEL",
        "LLM_API_KEY",
    }.issubset(variable_keys)
    assert (
        not {
            "DAILYCAST_LLM__PROVIDER",
            "DAILYCAST_LLM__BASE_URL",
            "DAILYCAST_LLM__MODEL",
            "DAILYCAST_LLM__API_KEY",
        }
        & variable_keys
    )
    assert not {
        "LLM_PROVIDER",
        "LLM_BASE_URL",
        "LLM_MODEL",
        "LLM_API_KEY",
    } & set(service_environment)


def test_zeabur_template_moves_fixed_runtime_values_into_deployment_yaml() -> None:
    """Only the config path and dynamic public origin belong in service environment rows."""
    template_path = Path(__file__).parents[1] / "zeabur.yaml"
    config_path = Path(__file__).parents[1] / "config" / "zeabur.yaml"
    resource = yaml.safe_load(template_path.read_text(encoding="utf-8"))
    service_environment = resource["spec"]["services"][0]["spec"]["env"]
    runtime_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert service_environment == {
        "DAILYCAST_CONFIG_PATH": {"default": "/app/config/zeabur.yaml"},
        "DAILYCAST_PUBLISHING__PUBLIC_BASE_URL": {"default": "${ZEABUR_WEB_URL}"},
    }
    assert runtime_config["app"] == {
        "environment": "production",
        "timezone": "Asia/Shanghai",
        "public_only": True,
        "server": {"host": "0.0.0.0", "port": 8000},
    }
    assert runtime_config["scheduler"] == {"enabled": True, "cron_expression": "0 6 * * *"}
    assert runtime_config["publishing"]["auto_publish"] is True
