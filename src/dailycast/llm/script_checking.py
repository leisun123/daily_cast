"""One-pass deterministic checking, semantic review, revision, and metadata workflow."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import ValidationError

from dailycast.core.errors import DailyCastError
from dailycast.llm.budget import BudgetController
from dailycast.llm.contracts import LLMUsage
from dailycast.llm.editorial_service import AIEditorialService
from dailycast.llm.outline_schemas import EpisodeOutline, EvidenceDossier
from dailycast.llm.script_review_editorial import (
    MetadataGenerationResult,
    ScriptReviewResult,
    ScriptRevisionResult,
)
from dailycast.llm.script_schemas import (
    EpisodeMetadata,
    EpisodeScript,
    ScriptReview,
    ValidationReport,
)
from dailycast.llm.script_validation import ScriptValidator


class ScriptStructuralValidationError(DailyCastError):
    """A stored script cannot safely enter semantic review or automatic revision."""

    def __init__(self) -> None:
        super().__init__(
            code="SCRIPT_STRUCTURAL_INVALID",
            message="script does not match the validated outline and evidence topology",
            status_code=422,
        )


@dataclass(frozen=True, slots=True)
class ScriptCheckingResult:
    """Final checked script state without creating an Episode or publishing an asset."""

    script: EpisodeScript
    validation: ValidationReport
    review: ScriptReview
    metadata: EpisodeMetadata | None
    automatic_revision_count: int
    requires_human_review: bool
    artifact_ids: tuple[int, ...]
    cache_hit_count: int
    usage: LLMUsage


class ScriptCheckingService:
    """Bound one semantic review/revision loop and preserve all results for human review."""

    def __init__(
        self,
        editorial_service: AIEditorialService,
        *,
        max_automatic_script_revisions: int = 1,
        enforce_quality_gate: bool = True,
    ) -> None:
        if max_automatic_script_revisions not in {0, 1}:
            msg = "Sprint 4B-3 permits at most one automatic script revision"
            raise ValueError(msg)
        self._editorial_service = editorial_service
        self._max_automatic_script_revisions = max_automatic_script_revisions
        self._enforce_quality_gate = enforce_quality_gate

    async def check(
        self,
        script: object,
        outline: object,
        evidence_dossiers: Sequence[object],
        *,
        selected_event_titles: Sequence[str],
        task_run_id: str,
        task_step_id: int,
        budget: BudgetController,
    ) -> ScriptCheckingResult:
        """Record checks, revising only when strict quality enforcement requires it."""
        validated_outline = EpisodeOutline.model_validate(outline)
        dossiers = tuple(EvidenceDossier.model_validate(dossier) for dossier in evidence_dossiers)
        validated_script = self._validated_script(script, validated_outline, dossiers)
        validation = self._editorial_service.validate_script(
            validated_script, validated_outline, dossiers
        )
        if ScriptValidator.has_structural_blocking_issues(validation):
            raise ScriptStructuralValidationError()

        review_result = await self._editorial_service.review_script(
            validated_script,
            dossiers,
            task_run_id=task_run_id,
            task_step_id=task_step_id,
            budget=budget,
        )
        revision_count = 0
        operation_results: list[
            ScriptReviewResult | ScriptRevisionResult | MetadataGenerationResult
        ] = [review_result]

        if (
            self._enforce_quality_gate
            and review_result.review.verdict == "revise"
            and self._max_automatic_script_revisions == 1
        ):
            revision_result = await self._editorial_service.revise_script(
                validated_script,
                validated_outline,
                dossiers,
                validation,
                review_result.review,
                task_run_id=task_run_id,
                task_step_id=task_step_id,
                budget=budget,
            )
            validated_script = revision_result.script
            validation = self._editorial_service.validate_script(
                validated_script, validated_outline, dossiers
            )
            if ScriptValidator.has_structural_blocking_issues(validation):
                raise ScriptStructuralValidationError()
            review_result = await self._editorial_service.review_script(
                validated_script,
                dossiers,
                task_run_id=task_run_id,
                task_step_id=task_step_id,
                budget=budget,
            )
            operation_results.extend((revision_result, review_result))
            revision_count = 1

        requires_human_review = (
            validation.has_blocking_issues
            or review_result.review.verdict != "pass"
            or any(issue.severity == "blocking" for issue in review_result.review.issues)
        )
        metadata_result = None
        if not requires_human_review or not self._enforce_quality_gate:
            metadata_result = await self._editorial_service.generate_metadata(
                validated_script,
                selected_event_titles,
                estimated_duration_seconds=validation.estimated_duration_seconds,
                task_run_id=task_run_id,
                task_step_id=task_step_id,
                budget=budget,
            )
            operation_results.append(metadata_result)

        return ScriptCheckingResult(
            script=validated_script,
            validation=validation,
            review=review_result.review,
            metadata=None if metadata_result is None else metadata_result.metadata,
            automatic_revision_count=revision_count,
            requires_human_review=requires_human_review,
            artifact_ids=tuple(
                result.artifact_id for result in operation_results if result.artifact_id is not None
            ),
            cache_hit_count=sum(bool(result.cache_hit) for result in operation_results),
            usage=LLMUsage(
                input_tokens=sum(result.usage.input_tokens for result in operation_results),
                output_tokens=sum(result.usage.output_tokens for result in operation_results),
            ),
        )

    @staticmethod
    def _validated_script(
        script: object,
        outline: EpisodeOutline,
        dossiers: tuple[EvidenceDossier, ...],
    ) -> EpisodeScript:
        """Reject a corrupted checkpoint before any new provider request can be attempted."""
        try:
            return EpisodeScript.model_validate(
                script,
                context={"outline": outline, "evidence_dossiers": dossiers},
            )
        except ValidationError as error:
            raise ScriptStructuralValidationError() from error
