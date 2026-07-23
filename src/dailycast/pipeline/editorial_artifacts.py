"""Controlled, atomic task-artifact storage for bounded editorial checkpoint outputs."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import UUID

from dailycast.llm.script_schemas import EpisodeScript

_EDITORIAL_FILENAMES = frozenset(
    {
        "outline.json",
        "script.json",
        "script.txt",
        "validation.json",
        "review.json",
        "metadata.json",
    }
)


class EditorialArtifactStore:
    """Write and read only approved relative editorial artifacts below the configured data root."""

    def __init__(self, data_dir: Path) -> None:
        self._root = data_dir.resolve()

    def write_json(self, task_run_id: str, filename: str, value: object) -> str:
        """Atomically persist canonical UTF-8 JSON for a named editorial checkpoint artifact."""
        target, relative_path = self._target(task_run_id, filename)
        self._atomic_write(target, _canonical_json(value))
        return relative_path.as_posix()

    def write_script_text(self, task_run_id: str, script: EpisodeScript) -> str:
        """Write only the text derived from a locally validated final EpisodeScript."""
        target, relative_path = self._target(task_run_id, "script.txt")
        rendered = "\n\n".join(section.text for section in script.sections)
        self._atomic_write(target, rendered)
        return relative_path.as_posix()

    def read_json(self, task_run_id: str, filename: str) -> dict[str, object]:
        """Read a controlled canonical object artifact and reject non-object or malformed files."""
        target, _ = self._target(task_run_id, filename)
        try:
            loaded = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            msg = f"editorial artifact is unavailable or invalid: {filename}"
            raise RuntimeError(msg) from error
        if not isinstance(loaded, dict):
            msg = f"editorial artifact must be a JSON object: {filename}"
            raise RuntimeError(msg)
        return loaded

    def _target(self, task_run_id: str, filename: str) -> tuple[Path, Path]:
        """Build one approved data-root-contained task artifact path."""
        try:
            normalized_task_run_id = str(UUID(task_run_id))
        except ValueError as error:
            msg = "task run ID must be a UUID for editorial artifact storage"
            raise RuntimeError(msg) from error
        if filename not in _EDITORIAL_FILENAMES:
            msg = f"unsupported editorial artifact filename: {filename}"
            raise RuntimeError(msg)
        relative_path = Path("work") / normalized_task_run_id / "editorial" / filename
        target = (self._root / relative_path).resolve()
        if not target.is_relative_to(self._root):
            msg = "editorial artifact path escaped the configured data directory"
            raise RuntimeError(msg)
        return target, relative_path

    @staticmethod
    def _atomic_write(target: Path, content: str) -> None:
        """Replace one destination only after a complete UTF-8 temporary file has been written."""
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, target)


def _canonical_json(value: object) -> str:
    """Encode stable compact JSON without prompts, secrets, or raw article bodies."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
