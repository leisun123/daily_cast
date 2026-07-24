"""Pipeline checkpoint that converts a validated outline into a bounded structured script."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from dailycast.llm.budget import BudgetController
from dailycast.llm.editorial_service import AIEditorialService
from dailycast.llm.outline_schemas import EpisodeOutline
from dailycast.pipeline.context import PipelineContext
from dailycast.pipeline.contracts import StepResult
from dailycast.pipeline.editorial_artifacts import EditorialArtifactStore


@dataclass(frozen=True, slots=True)
class ScriptingStep:
    """Generate one schema-valid script from an outline artifact and rebuilt bounded dossiers."""

    editorial_service: AIEditorialService
    data_dir: Path
    budget_factory: Callable[[], BudgetController] = BudgetController
    name: str = "scripting"

    async def run(self, context: PipelineContext) -> StepResult:
        """Persist canonical scripts after Artifact validation accepts the LLM output."""
        task_step_id = _active_task_step_id(context)
        event_ids = _event_ids(context.values.get("outlined_news_event_ids"))
        store = EditorialArtifactStore(self.data_dir)
        outline = EpisodeOutline.model_validate(
            store.read_json(context.artifact_run_id, "outline.json")
        )
        built = self.editorial_service.build_evidence_dossiers(event_ids)
        budget = _task_budget(context, self.budget_factory)
        generated = await self.editorial_service.generate_script(
            outline,
            built.dossiers,
            task_run_id=context.task_run_id,
            task_step_id=task_step_id,
            budget=budget,
        )
        validation = self.editorial_service.validate_script(
            generated.script, outline, built.dossiers
        )
        script_path = store.write_json(
            context.artifact_run_id,
            "script.json",
            generated.script.model_dump(mode="json"),
        )
        store.write_script_text(context.artifact_run_id, generated.script)
        context.values["episode_outline"] = outline
        context.values["episode_script"] = generated.script
        context.values["outlined_news_event_ids"] = built.event_ids
        return StepResult(
            input_count=len(built.event_ids),
            output_count=len(generated.script.sections),
            checkpoint_json=_canonical_json(
                {
                    "artifact_id": generated.artifact_id,
                    "script_schema_version": generated.script.schema_version,
                    "validation_issue_count": len(validation.issues),
                }
            ),
            details={
                "artifact_id": generated.artifact_id,
                "cache_hit": generated.cache_hit,
                "script_section_count": len(generated.script.sections),
                "script_character_count": validation.character_count,
                "estimated_duration_seconds": validation.estimated_duration_seconds,
                "llm_input_tokens": generated.usage.input_tokens,
                "llm_output_tokens": generated.usage.output_tokens,
            },
            artifact_path=script_path,
            llm_call_count=generated.provider_call_count,
            llm_input_tokens=generated.usage.input_tokens if not generated.cache_hit else 0,
            llm_output_tokens=generated.usage.output_tokens if not generated.cache_hit else 0,
        )


def _active_task_step_id(context: PipelineContext) -> int:
    """Require the orchestrator-created TaskStep used for all Artifact provenance."""
    task_step_id = context.values.get("active_task_step_id")
    if not isinstance(task_step_id, int):
        msg = "scripting requires an active persisted TaskStep ID"
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
    """Reuse the one TaskRun budget already allocated by ranking or outlining."""
    existing = context.values.get("llm_budget")
    if isinstance(existing, BudgetController):
        return existing
    budget = budget_factory()
    context.values["llm_budget"] = budget
    return budget


def _canonical_json(value: object) -> str:
    """Encode a compact stable TaskStep checkpoint without including prompts or secrets."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
