"""Artifact-backed generation of a bounded, traceable structured podcast script."""

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
from dailycast.llm.script_schemas import GENERATE_SCRIPT_V1_SCHEMA_VERSION, EpisodeScript


@dataclass(frozen=True, slots=True)
class ScriptGenerationResult:
    """One cacheable validated EpisodeScript plus its durable Artifact provenance."""

    script: EpisodeScript
    artifact_id: int | None
    cache_hit: bool
    usage: LLMUsage


async def generate_script(
    session_factory: sessionmaker[Session],
    provider: LLMProvider,
    outline: EpisodeOutline,
    evidence_dossiers: Sequence[EvidenceDossier],
    *,
    task_run_id: str,
    task_step_id: int,
    budget: BudgetController,
    model_options: Mapping[str, JSONValue],
    prompt: PromptTemplate,
) -> ScriptGenerationResult:
    """Generate or reuse one strict EpisodeScript without sending complete article bodies."""
    dossiers = tuple(EvidenceDossier.model_validate(dossier) for dossier in evidence_dossiers)
    validation_context: dict[str, object] = {
        "outline": outline,
        "evidence_dossiers": dossiers,
    }
    messages = _script_messages(outline, dossiers, prompt)
    artifact_service = LLMArtifactService(session_factory, provider, budget)
    structured_result = await artifact_service.generate_structured(
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
    script = EpisodeScript.model_validate(structured_result.content, context=validation_context)
    return ScriptGenerationResult(
        script=script,
        artifact_id=structured_result.artifact_id,
        cache_hit=structured_result.cache_hit,
        usage=structured_result.usage,
    )


def _script_messages(
    outline: EpisodeOutline,
    dossiers: Sequence[EvidenceDossier],
    prompt: PromptTemplate,
) -> tuple[LLMMessage, ...]:
    """Build the canonical outline-plus-excerpt request without raw bodies or stored prompts."""
    payload = {
        "output_constraints": _script_output_constraints(outline, dossiers),
        "outline": outline.model_dump(mode="json"),
        "evidence_dossiers": [dossier.model_dump(mode="json") for dossier in dossiers],
    }
    return (
        LLMMessage(role="system", content=prompt.system_instruction),
        LLMMessage(
            role="user",
            content=json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        ),
    )


def _script_output_constraints(
    outline: EpisodeOutline, dossiers: Sequence[EvidenceDossier]
) -> dict[str, object]:
    """Expose exact per-section reference allowlists instead of asking the model to infer them."""
    article_ids_by_event = {
        dossier.event_id: tuple(source.article_id for source in dossier.evidence_sources)
        for dossier in dossiers
    }
    allowed_references = []
    for section in outline.sections:
        allowed_article_ids = tuple(
            sorted(
                {
                    article_id
                    for event_id in section.event_ids
                    for article_id in article_ids_by_event.get(event_id, ())
                }
            )
        )
        allowed_references.append(
            {
                "section_id": section.section_id,
                "section_type": section.type,
                "allowed_event_ids": list(section.event_ids),
                "allowed_article_ids": list(allowed_article_ids),
            }
        )
    return {
        "schema_version": "1",
        "required_section_ids": [section.section_id for section in outline.sections],
        "allowed_references_by_section": allowed_references,
    }
