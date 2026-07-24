"""Readiness probes for the Sprint 0 runtime dependencies."""

import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from sqlalchemy import Engine, text

from dailycast.core.config import Settings
from dailycast.db.revision import RevisionStatus


class RuntimeForReadiness(Protocol):
    """Runtime attributes required by the readiness probe."""

    @property
    def settings(self) -> Settings:
        """Return validated application settings."""

    @property
    def engine(self) -> Engine:
        """Return the initialized SQLite engine."""

    @property
    def startup_revision_status(self) -> RevisionStatus | None:
        """Return the revision comparison captured during application startup."""

    @property
    def startup_revision_error(self) -> str | None:
        """Return a safe startup revision inspection failure, if any."""

    @property
    def executor(self) -> object | None:
        """Return the single in-process task executor when the current schema is usable."""


@dataclass(frozen=True)
class CheckResult:
    """One named readiness check with an operator-safe detail string."""

    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class ReadinessReport:
    """Aggregate result returned by the `/readyz` endpoint."""

    checks: tuple[CheckResult, ...]

    @property
    def ready(self) -> bool:
        """Return true only when every required local dependency is ready."""
        return all(check.ok for check in self.checks)

    def as_dict(self) -> dict[str, object]:
        """Return the JSON-safe health response body."""
        return {
            "status": "ready" if self.ready else "not_ready",
            "checks": [asdict(check) for check in self.checks],
        }


def _check_writable(name: str, directory: Path) -> CheckResult:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=directory):
            pass
    except OSError as error:
        return CheckResult(name=name, ok=False, detail=f"not writable: {error}")
    return CheckResult(name=name, ok=True, detail="writable")


def _check_ffmpeg() -> CheckResult:
    executable = shutil.which("ffmpeg")
    if executable is None:
        return CheckResult(name="ffmpeg", ok=False, detail="ffmpeg was not found on PATH")
    try:
        completed = subprocess.run(
            [executable, "-version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return CheckResult(name="ffmpeg", ok=False, detail=f"ffmpeg probe failed: {error}")
    if completed.returncode != 0:
        return CheckResult(name="ffmpeg", ok=False, detail="ffmpeg returned a non-zero status")
    return CheckResult(name="ffmpeg", ok=True, detail="available")


def evaluate_readiness(runtime: RuntimeForReadiness) -> ReadinessReport:
    """Evaluate all Sprint 0 runtime checks without mutating migration state."""
    settings = runtime.settings
    engine = runtime.engine
    checks: list[CheckResult] = [CheckResult(name="config", ok=True, detail="loaded")]

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as error:  # SQLAlchemy surface is provider-specific.
        checks.append(CheckResult(name="sqlite", ok=False, detail=f"unreachable: {error}"))
    else:
        checks.append(CheckResult(name="sqlite", ok=True, detail="reachable"))

    if runtime.startup_revision_error is not None:
        checks.append(
            CheckResult(
                name="alembic_revision",
                ok=False,
                detail=f"unavailable: {runtime.startup_revision_error}",
            )
        )
    elif runtime.startup_revision_status is None:
        checks.append(
            CheckResult(
                name="alembic_revision",
                ok=False,
                detail="startup revision status is unavailable",
            )
        )
    else:
        revision = runtime.startup_revision_status
        checks.append(
            CheckResult(
                name="alembic_revision",
                ok=revision.is_current,
                detail=f"current={list(revision.current)}, expected={list(revision.expected)}",
            )
        )

    checks.append(_check_writable("data_dir", settings.data_dir))
    checks.append(_check_writable("public_dir", settings.public_dir))
    checks.append(_check_ffmpeg())
    executor = runtime.executor
    worker_healthy = bool(getattr(executor, "is_healthy", False))
    worker_detail = (
        str(getattr(executor, "readiness_detail", "single worker is not available"))
        if executor is not None
        else "single worker is not initialized"
    )
    checks.append(CheckResult(name="task_worker", ok=worker_healthy, detail=worker_detail))
    return ReadinessReport(checks=tuple(checks))
