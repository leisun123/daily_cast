"""End-to-end Docker Compose startup test for Sprint 0."""

import os
import socket
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.docker


def _unused_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as socket_handle:
        socket_handle.bind(("127.0.0.1", 0))
        return int(socket_handle.getsockname()[1])


@pytest.mark.skipif(
    os.environ.get("DAILYCAST_RUN_DOCKER_TEST") != "1",
    reason="set DAILYCAST_RUN_DOCKER_TEST=1 to run Docker Compose startup verification",
)
def test_compose_starts_and_serves_healthz(tmp_path: Path) -> None:
    """Compose runs Alembic first, then exposes a healthy FastAPI container."""
    project_root = Path(__file__).resolve().parents[2]
    project_name = f"dailycast-sprint0-{uuid.uuid4().hex[:8]}"
    port = _unused_local_port()
    environment = os.environ.copy()
    environment.update(
        {
            "DAILYCAST_HOST_PORT": str(port),
            "DAILYCAST_DATA_DIR": str(tmp_path / "data"),
            "DAILYCAST_PUBLIC_DIR": str(tmp_path / "public"),
            "DAILYCAST_UID": str(os.getuid()),
            "DAILYCAST_GID": str(os.getgid()),
        }
    )
    compose = ["docker", "compose", "-p", project_name, "-f", "compose.yaml"]
    response: httpx.Response | None = None

    try:
        subprocess.run(
            [*compose, "up", "--build", "--detach"],
            cwd=project_root,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                response = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=2)
            except httpx.HTTPError:
                time.sleep(1)
                continue
            if response.status_code == 200:
                break
            time.sleep(1)
        else:
            logs = subprocess.run(
                [*compose, "logs", "--no-color"],
                cwd=project_root,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            pytest.fail(f"container did not serve /healthz:\n{logs.stdout}\n{logs.stderr}")

        assert response is not None
        assert response.json() == {"status": "ok"}
        assert httpx.get(f"http://127.0.0.1:{port}/readyz", timeout=5).status_code == 200
    finally:
        subprocess.run(
            [*compose, "down", "--volumes", "--remove-orphans"],
            cwd=project_root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
