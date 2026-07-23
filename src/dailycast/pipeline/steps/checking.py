"""Pipeline checkpoint for deterministic validation, bounded review, and one controlled revision."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from dailycast.llm.budget import BudgetController
from dailycast.llm.editorial_service import AIEditorialService
from dailycast.llm.outline_schemas import EpisodeOutline
from dailycast.llm.script_checking import ScriptCheckingService
from dailycast.llm.script_schemas import EpisodeScript
from dailycast.pipeline.context import PipelineContext
from dailycast.pipeline.contracts import JSONValue, StepResult
from dailycast.pipeline.editorial_artifacts import EditorialArtifactStore


@dataclass(frozen=True, slots=True)
class CheckingStep:
    """Run final pre-Episode checks without creating episode, audio, RSS, or publication rows."""

    editorial_service: AIEditorialService
    data_dir: Path
    budget_factory: Callable[[], BudgetController] = BudgetController
    max_automatic_script_revisions: int = 1
    enforce_quality_gate: bool = True
    name: str = "checking"

    async def run(self, context: PipelineContext) -> StepResult:
        """Write validation/review outputs and preserve a review-required script as a warning."""
        task_step_id = _active_task_step_id(context)
        store = EditorialArtifactStore(self.data_dir)
        event_ids = _event_ids(context.values.get("outlined_news_event_ids"))
        outline = EpisodeOutline.model_validate(
            store.read_json(context.task_run_id, "outline.json")
        )
        built = self.editorial_service.build_evidence_dossiers(event_ids)
        script = EpisodeScript.model_validate(store.read_json(context.task_run_id, "script.json"))
        result = await ScriptCheckingService(
            self.editorial_service,
            max_automatic_script_revisions=self.max_automatic_script_revisions,
            enforce_quality_gate=self.enforce_quality_gate,
        ).check(
            script,
            outline,
            built.dossiers,
            selected_event_titles=[dossier.title for dossier in built.dossiers],
            task_run_id=context.task_run_id,
            task_step_id=task_step_id,
            budget=_task_budget(context, self.budget_factory),
        )
        store.write_json(context.task_run_id, "script.json", result.script.model_dump(mode="json"))
        store.write_script_text(context.task_run_id, result.script)
        store.write_json(
            context.task_run_id, "validation.json", result.validation.model_dump(mode="json")
        )
        review_path = store.write_json(
            context.task_run_id,
            "review.json",
            result.review.model_dump(mode="json"),
        )
        if result.metadata is not None:
            store.write_json(
                context.task_run_id,
                "metadata.json",
                result.metadata.model_dump(mode="json"),
            )
        issue_counts: dict[str, JSONValue] = {
            "blocking": sum(issue.severity == "blocking" for issue in result.validation.issues),
            "warning": sum(issue.severity == "warning" for issue in result.validation.issues),
        }
        context.values["episode_script"] = result.script
        context.values["script_validation"] = result.validation
        context.values["script_review"] = result.review
        context.values["episode_metadata"] = result.metadata
        context.values["episode_outline"] = outline
        context.values["evidence_dossiers"] = built.dossiers
        return StepResult(
            input_count=len(built.event_ids),
            output_count=1 if result.metadata is not None else 0,
            warning_count=1 if result.requires_human_review else 0,
            checkpoint_json=_canonical_json(
                {
                    "review_verdict": result.review.verdict,
                    "requires_human_review": result.requires_human_review,
                    "automatic_revision_count": result.automatic_revision_count,
                    "artifact_ids": list(result.artifact_ids),
                }
            ),
            details={
                "artifact_ids": list(result.artifact_ids),
                "cache_hit_count": result.cache_hit_count,
                "script_section_count": len(result.script.sections),
                "script_character_count": result.validation.character_count,
                "estimated_duration_seconds": result.validation.estimated_duration_seconds,
                "validation_issue_counts": issue_counts,
                "review_verdict": result.review.verdict,
                "automatic_revision_count": result.automatic_revision_count,
                "requires_human_review": result.requires_human_review,
                "llm_input_tokens": result.usage.input_tokens,
                "llm_output_tokens": result.usage.output_tokens,
            },
            artifact_path=review_path,
        )


def _active_task_step_id(context: PipelineContext) -> int:
    """Require the orchestrator-created TaskStep used for the checking Artifact provenance."""
    task_step_id = context.values.get("active_task_step_id")
    if not isinstance(task_step_id, int):
        msg = "checking requires an active persisted TaskStep ID"
        raise RuntimeError(msg)
    return task_step_id


def _event_ids(value: object) -> tuple[int, ...]:
    """Accept only durable selected IDs written by the outlining checkpoint."""
    if isinstance(value, tuple) and all(isinstance(event_id, int) for event_id in value):
        return value
    return ()


def _task_budget(
    context: PipelineContext, budget_factory: Callable[[], BudgetController]
) -> BudgetController:
    """Reuse the one TaskRun budget already allocated by ranking, outline, or scripting."""
    existing = context.values.get("llm_budget")
    if isinstance(existing, BudgetController):
        return existing
    budget = budget_factory()
    context.values["llm_budget"] = budget
    return budget


def _canonical_json(value: object) -> str:
    """Encode a compact stable TaskStep checkpoint without including prompts or secrets."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
