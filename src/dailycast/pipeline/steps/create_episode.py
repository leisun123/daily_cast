"""Pipeline checkpoint that persists an accepted editorial result as one Episode draft."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from dailycast.db.repositories import TaskRunRepository
from dailycast.db.transactions import UnitOfWork
from dailycast.episodes.service import EpisodeService
from dailycast.llm.outline_schemas import EpisodeOutline
from dailycast.llm.script_schemas import (
    EpisodeMetadata,
    EpisodeScript,
    ScriptReview,
    ValidationReport,
)
from dailycast.pipeline.context import PipelineContext
from dailycast.pipeline.contracts import JSONValue, StepResult
from dailycast.pipeline.editorial_artifacts import EditorialArtifactStore


@dataclass(frozen=True, slots=True)
class CreateEpisodeStep:
    """Persist only editorial artifacts that passed local validation and semantic review."""

    episode_service: EpisodeService
    data_dir: Path
    name: str = "create_episode"

    async def run(self, context: PipelineContext) -> StepResult:
        """Create one idempotent Episode snapshot or record an ineligible editorial result."""
        _active_task_step_id(context)
        store = EditorialArtifactStore(self.data_dir)
        outline = EpisodeOutline.model_validate(
            store.read_json(context.task_run_id, "outline.json")
        )
        script = EpisodeScript.model_validate(store.read_json(context.task_run_id, "script.json"))
        validation = ValidationReport.model_validate(
            store.read_json(context.task_run_id, "validation.json")
        )
        review = ScriptReview.model_validate(store.read_json(context.task_run_id, "review.json"))
        selected_event_ids = _event_ids(context.values.get("outlined_news_event_ids"))
        artifact_refs = _artifact_refs(context.task_run_id)
        if validation.has_blocking_issues:
            return _skipped_result(selected_event_ids, artifact_refs, "SCRIPT_VALIDATION_FAILED")
        if review.verdict != "pass" or any(issue.severity == "blocking" for issue in review.issues):
            return _skipped_result(selected_event_ids, artifact_refs, "EDITORIAL_REVIEW_NOT_PASS")
        if not selected_event_ids:
            return _skipped_result(selected_event_ids, artifact_refs, "NO_SELECTED_EVENTS")
        evidence_dossiers = context.values.get("evidence_dossiers")
        if not isinstance(evidence_dossiers, tuple):
            return _skipped_result(
                selected_event_ids, artifact_refs, "EVIDENCE_DOSSIERS_UNAVAILABLE"
            )
        try:
            metadata = EpisodeMetadata.model_validate(
                store.read_json(context.task_run_id, "metadata.json")
            )
        except RuntimeError:
            return _skipped_result(selected_event_ids, artifact_refs, "METADATA_UNAVAILABLE")
        episode_date, edition = self._episode_identity(context)
        episode = self.episode_service.create_from_editorial_artifacts(
            episode_date=episode_date,
            edition=edition,
            outline=outline,
            script=script,
            validation=validation,
            review=review,
            metadata=metadata,
            selected_event_ids=selected_event_ids,
            evidence_dossiers=evidence_dossiers,
            task_run_id=context.task_run_id,
        )
        context.values["episode_id"] = episode.id
        return StepResult(
            input_count=len(selected_event_ids),
            output_count=1,
            checkpoint_json=_canonical_json(
                {
                    "artifact_refs": artifact_refs,
                    "episode_id": episode.id,
                    "episode_public_id": episode.public_id,
                }
            ),
            details={
                "artifact_refs": artifact_refs,
                "episode_id": episode.id,
                "episode_public_id": episode.public_id,
                "episode_status": episode.status.value,
                "script_revision": episode.script_revision,
            },
            artifact_path=f"work/{context.task_run_id}/editorial/metadata.json",
        )

    def _episode_identity(self, context: PipelineContext) -> tuple[date, str]:
        """Read the durable business identity from the submitted TaskRun, not untrusted files."""
        with UnitOfWork(context.session_factory) as unit:
            assert unit.session is not None
            task_run = TaskRunRepository(unit.session).get(context.task_run_id)
            if task_run is None:
                msg = f"TaskRun {context.task_run_id} does not exist"
                raise RuntimeError(msg)
            try:
                request = json.loads(task_run.request_json)
            except json.JSONDecodeError as error:
                raise RuntimeError("TaskRun request JSON is invalid") from error
        if not isinstance(request, dict):
            raise RuntimeError("TaskRun request must be a JSON object")
        raw_date = request.get("episode_date")
        raw_edition = request.get("edition", "daily")
        if not isinstance(raw_date, str) or not isinstance(raw_edition, str) or not raw_edition:
            raise RuntimeError("TaskRun request must contain episode_date and edition")
        try:
            return date.fromisoformat(raw_date), raw_edition
        except ValueError as error:
            raise RuntimeError("TaskRun episode_date must use YYYY-MM-DD") from error


def _active_task_step_id(context: PipelineContext) -> int:
    """Require the orchestrator-created TaskStep that will persist this checkpoint result."""
    task_step_id = context.values.get("active_task_step_id")
    if not isinstance(task_step_id, int):
        raise RuntimeError("create_episode requires an active persisted TaskStep ID")
    return task_step_id


def _event_ids(value: object) -> tuple[int, ...]:
    """Accept only selected durable IDs from the outlining checkpoint."""
    if isinstance(value, tuple) and value and all(isinstance(event_id, int) for event_id in value):
        return value
    return ()


def _artifact_refs(task_run_id: str) -> list[JSONValue]:
    """Return the controlled paths that make the persisted Episode replayable and auditable."""
    root = f"work/{task_run_id}/editorial"
    return [
        f"{root}/outline.json",
        f"{root}/script.json",
        f"{root}/validation.json",
        f"{root}/review.json",
        f"{root}/metadata.json",
    ]


def _skipped_result(
    selected_event_ids: tuple[int, ...], artifact_refs: list[JSONValue], reason: str
) -> StepResult:
    """Keep rejected editorial work visible without manufacturing an incomplete Episode."""
    return StepResult(
        input_count=len(selected_event_ids),
        output_count=0,
        warning_count=1,
        checkpoint_json=_canonical_json(
            {"artifact_refs": artifact_refs, "episode_created": False, "skip_reason": reason}
        ),
        details={"artifact_refs": artifact_refs, "episode_created": False, "skip_reason": reason},
    )


def _canonical_json(value: object) -> str:
    """Encode a stable JSON TaskStep checkpoint without copying source article bodies."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
