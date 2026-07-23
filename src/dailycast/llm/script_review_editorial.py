"""Artifact-backed semantic script review, bounded one-shot revision, and metadata generation."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from dailycast.db.models import LLMOperation
from dailycast.llm.artifacts import LLMArtifactService
from dailycast.llm.budget import BudgetController
from dailycast.llm.contracts import JSONValue, LLMMessage, LLMProvider, LLMUsage
from dailycast.llm.outline_schemas import EpisodeOutline, EvidenceDossier
from dailycast.llm.prompts import PromptTemplate
from dailycast.llm.script_schemas import (
    GENERATE_METADATA_V1_SCHEMA_VERSION,
    GENERATE_SCRIPT_V1_SCHEMA_VERSION,
    REVIEW_SCRIPT_V1_SCHEMA_VERSION,
    EpisodeMetadata,
    EpisodeScript,
    ScriptReview,
    ValidationReport,
)


@dataclass(frozen=True, slots=True)
class ScriptReviewResult:
    """One cacheable schema-valid semantic review plus LLMArtifact provenance."""

    review: ScriptReview
    artifact_id: int | None
    cache_hit: bool
    usage: LLMUsage


@dataclass(frozen=True, slots=True)
class ScriptRevisionResult:
    """One controlled revised script with durable Artifact provenance."""

    script: EpisodeScript
    artifact_id: int | None
    cache_hit: bool
    usage: LLMUsage


@dataclass(frozen=True, slots=True)
class MetadataGenerationResult:
    """One cacheable plain-text metadata result plus Artifact provenance."""

    metadata: EpisodeMetadata
    artifact_id: int | None
    cache_hit: bool
    usage: LLMUsage


async def review_script(
    session_factory: sessionmaker[Session],
    provider: LLMProvider,
    script: EpisodeScript,
    evidence_dossiers: Sequence[EvidenceDossier],
    *,
    task_run_id: str,
    task_step_id: int,
    budget: BudgetController,
    model_options: Mapping[str, JSONValue],
    prompt: PromptTemplate,
) -> ScriptReviewResult:
    """Generate or reuse one strict evidence-bounded semantic review."""
    dossiers = tuple(EvidenceDossier.model_validate(dossier) for dossier in evidence_dossiers)
    validation_context: dict[str, object] = {"script": script, "evidence_dossiers": dossiers}
    messages = _review_messages(script, dossiers, prompt)
    structured_result = await LLMArtifactService(
        session_factory, provider, budget
    ).generate_structured(
        operation=LLMOperation.REVIEW_SCRIPT,
        messages=messages,
        response_schema=ScriptReview,
        prompt_version=prompt.version,
        schema_version=REVIEW_SCRIPT_V1_SCHEMA_VERSION,
        model_options=model_options,
        created_by_task_run_id=task_run_id,
        created_by_task_step_id=task_step_id,
        validation_context=validation_context,
    )
    review = ScriptReview.model_validate(structured_result.content, context=validation_context)
    return ScriptReviewResult(
        review=review,
        artifact_id=structured_result.artifact_id,
        cache_hit=structured_result.cache_hit,
        usage=structured_result.usage,
    )


async def revise_script(
    session_factory: sessionmaker[Session],
    provider: LLMProvider,
    script: EpisodeScript,
    outline: EpisodeOutline,
    evidence_dossiers: Sequence[EvidenceDossier],
    validation_report: ValidationReport,
    review: ScriptReview,
    *,
    task_run_id: str,
    task_step_id: int,
    budget: BudgetController,
    model_options: Mapping[str, JSONValue],
    prompt: PromptTemplate,
) -> ScriptRevisionResult:
    """Generate a locally schema-validated revision for one checking attempt."""
    dossiers = tuple(EvidenceDossier.model_validate(dossier) for dossier in evidence_dossiers)
    validation_context: dict[str, object] = {"outline": outline, "evidence_dossiers": dossiers}
    payload = {
        "outline": outline.model_dump(mode="json"),
        "script": script.model_dump(mode="json"),
        "evidence_dossiers": [dossier.model_dump(mode="json") for dossier in dossiers],
        "deterministic_validation": validation_report.model_dump(mode="json"),
        "semantic_review": review.model_dump(mode="json"),
    }
    messages = _messages(prompt, payload)
    structured_result = await LLMArtifactService(
        session_factory, provider, budget
    ).generate_structured(
        operation=LLMOperation.GENERATE_SCRIPT,
        messages=messages,
        response_schema=EpisodeScript,
        prompt_version=prompt.version,
        schema_version=GENERATE_SCRIPT_V1_SCHEMA_VERSION,
        model_options=model_options,
        created_by_task_run_id=task_run_id,
        created_by_task_step_id=task_step_id,
        validation_context=validation_context,
    )
    revised_script = EpisodeScript.model_validate(
        structured_result.content, context=validation_context
    )
    return ScriptRevisionResult(
        script=revised_script,
        artifact_id=structured_result.artifact_id,
        cache_hit=structured_result.cache_hit,
        usage=structured_result.usage,
    )


async def generate_metadata(
    session_factory: sessionmaker[Session],
    provider: LLMProvider,
    script: EpisodeScript,
    selected_event_titles: Sequence[str],
    *,
    estimated_duration_seconds: float,
    task_run_id: str,
    task_step_id: int,
    budget: BudgetController,
    model_options: Mapping[str, JSONValue],
    prompt: PromptTemplate,
) -> MetadataGenerationResult:
    """Generate bounded metadata without resending full article text."""
    payload = {
        "selected_event_titles": [title for title in selected_event_titles],
        "script_text": _bounded_script_text(script),
        "estimated_duration_seconds": round(estimated_duration_seconds, 2),
    }
    structured_result = await LLMArtifactService(
        session_factory, provider, budget
    ).generate_structured(
        operation=LLMOperation.GENERATE_METADATA,
        messages=_messages(prompt, payload),
        response_schema=EpisodeMetadata,
        prompt_version=prompt.version,
        schema_version=GENERATE_METADATA_V1_SCHEMA_VERSION,
        model_options=model_options,
        created_by_task_run_id=task_run_id,
        created_by_task_step_id=task_step_id,
        validation_context={
            "script": script,
            "selected_event_titles": tuple(selected_event_titles),
        },
    )
    metadata = EpisodeMetadata.model_validate(
        structured_result.content,
        context={"script": script, "selected_event_titles": tuple(selected_event_titles)},
    )
    return MetadataGenerationResult(
        metadata=metadata,
        artifact_id=structured_result.artifact_id,
        cache_hit=structured_result.cache_hit,
        usage=structured_result.usage,
    )


def _review_messages(
    script: EpisodeScript,
    dossiers: Sequence[EvidenceDossier],
    prompt: PromptTemplate,
) -> tuple[LLMMessage, ...]:
    """Send the reviewed script and only bounded cited evidence to the semantic review operation."""
    return _messages(
        prompt,
        {
            "script": script.model_dump(mode="json"),
            "evidence_dossiers": [dossier.model_dump(mode="json") for dossier in dossiers],
        },
    )


def _messages(prompt: PromptTemplate, payload: object) -> tuple[LLMMessage, ...]:
    """Encode a stable bounded structured payload without retaining prompts in Artifact rows."""
    return (
        LLMMessage(role="system", content=prompt.system_instruction),
        LLMMessage(
            role="user",
            content=json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        ),
    )


def _bounded_script_text(script: EpisodeScript) -> str:
    """Limit metadata input to a stable plain-text projection rather than all editorial evidence."""
    text = "\n".join(section.text for section in script.sections)
    return " ".join(text.split())[:4000]
