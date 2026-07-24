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
    assert spec["env"]["DAILYCAST_APP__PUBLIC_ONLY"]["default"] == "true"
    assert spec["env"]["DAILYCAST_SCHEDULER__ENABLED"]["default"] == "true"
    assert spec["env"]["DAILYCAST_PUBLISHING__AUTO_PUBLISH"]["default"] == "true"
    assert (
        spec["env"]["DAILYCAST_PUBLISHING__PUBLIC_BASE_URL"]["default"]
        == "${ZEABUR_WEB_URL}"
    )
