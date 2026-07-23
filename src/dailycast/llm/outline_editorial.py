"""Bounded EvidenceDossier construction and artifact-backed outline generation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload, sessionmaker

from dailycast.core.errors import DailyCastError
from dailycast.db.models import Article, LLMOperation, NewsEvent, NewsEventStatus
from dailycast.db.transactions import UnitOfWork
from dailycast.llm.artifacts import LLMArtifactService
from dailycast.llm.budget import BudgetController
from dailycast.llm.contracts import JSONValue, LLMMessage, LLMProvider, LLMUsage
from dailycast.llm.outline_schemas import (
    GENERATE_OUTLINE_V1_SCHEMA_VERSION,
    EpisodeOutline,
    EvidenceDossier,
    EvidenceSource,
)
from dailycast.llm.prompts import PromptTemplate


class InsufficientEvidenceError(DailyCastError):
    """Raised when bounded source material cannot support the configured minimum episode scope."""

    def __init__(self) -> None:
        super().__init__(
            code="EVIDENCE_DOSSIER_INSUFFICIENT",
            message="selected events do not have enough bounded evidence for an outline",
            status_code=422,
        )


@dataclass(frozen=True, slots=True)
class EvidenceDossierBuildResult:
    """Dossiers and audit counts produced after applying every evidence-size limit."""

    event_ids: tuple[int, ...]
    dossiers: tuple[EvidenceDossier, ...]
    source_article_count: int
    total_evidence_chars: int


@dataclass(frozen=True, slots=True)
class OutlineGenerationResult:
    """One cached or newly generated schema-valid episode outline."""

    outline: EpisodeOutline
    artifact_id: int | None
    cache_hit: bool
    usage: LLMUsage


def build_evidence_dossiers(
    session_factory: sessionmaker[Session],
    event_ids: Sequence[int],
    *,
    max_sources_per_event: int,
    max_chars_per_source: int,
    max_total_evidence_chars: int,
    min_publishable_events: int,
) -> EvidenceDossierBuildResult:
    """Load selected events and deterministically build a fully bounded evidence-only payload."""
    _validate_evidence_limits(
        max_sources_per_event=max_sources_per_event,
        max_chars_per_source=max_chars_per_source,
        max_total_evidence_chars=max_total_evidence_chars,
        min_publishable_events=min_publishable_events,
    )
    events = _load_selected_events(session_factory, event_ids)
    if len(events) < min_publishable_events:
        raise InsufficientEvidenceError()
    dossiers = tuple(
        _event_dossier(
            event, max_sources_per_event=max_sources_per_event, max_chars=max_chars_per_source
        )
        for event in events
    )
    bounded = _fit_total_evidence_budget(
        dossiers,
        max_total_evidence_chars=max_total_evidence_chars,
        min_publishable_events=min_publishable_events,
    )
    source_article_ids = {
        source.article_id for dossier in bounded for source in dossier.evidence_sources
    }
    return EvidenceDossierBuildResult(
        event_ids=tuple(dossier.event_id for dossier in bounded),
        dossiers=bounded,
        source_article_count=len(source_article_ids),
        total_evidence_chars=_total_evidence_chars(bounded),
    )


async def generate_outline(
    session_factory: sessionmaker[Session],
    provider: LLMProvider,
    selected_event_ids: Sequence[int],
    dossiers: Sequence[EvidenceDossier],
    *,
    task_run_id: str,
    task_step_id: int,
    budget: BudgetController,
    model_options: Mapping[str, JSONValue],
    prompt: PromptTemplate,
    target_duration_seconds: int,
    duration_tolerance_seconds: int,
    max_outline_sections: int,
) -> OutlineGenerationResult:
    """Cache and validate an outline against exactly the supplied selected-event evidence."""
    event_ids = _unique_event_ids(selected_event_ids)
    _validate_outline_limits(
        event_ids=event_ids,
        target_duration_seconds=target_duration_seconds,
        duration_tolerance_seconds=duration_tolerance_seconds,
        max_outline_sections=max_outline_sections,
    )
    normalized_dossiers = tuple(EvidenceDossier.model_validate(dossier) for dossier in dossiers)
    if tuple(dossier.event_id for dossier in normalized_dossiers) != event_ids:
        msg = "outline dossiers must cover the supplied selected event IDs in deterministic order"
        raise ValueError(msg)
    for dossier in normalized_dossiers:
        for source in dossier.evidence_sources:
            if len(source.text_excerpt) > 1200:
                msg = "outline evidence source exceeded the hard schema character limit"
                raise ValueError(msg)
    messages = _outline_messages(
        normalized_dossiers,
        target_duration_seconds=target_duration_seconds,
        duration_tolerance_seconds=duration_tolerance_seconds,
        max_outline_sections=max_outline_sections,
        prompt=prompt,
    )
    validation_context: dict[str, object] = {
        "selected_event_ids": event_ids,
        "target_duration_seconds": target_duration_seconds,
        "duration_tolerance_seconds": duration_tolerance_seconds,
        "max_outline_sections": max_outline_sections,
    }
    artifact_service = LLMArtifactService(session_factory, provider, budget)
    structured_result = await artifact_service.generate_structured(
        operation=LLMOperation.GENERATE_OUTLINE,
        messages=messages,
        response_schema=EpisodeOutline,
        prompt_version=prompt.version,
        schema_version=GENERATE_OUTLINE_V1_SCHEMA_VERSION,
        model_options=model_options,
        created_by_task_run_id=task_run_id,
        created_by_task_step_id=task_step_id,
        validation_context=validation_context,
    )
    outline = EpisodeOutline.model_validate(
        structured_result.content,
        context=validation_context,
    )
    return OutlineGenerationResult(
        outline=outline,
        artifact_id=structured_result.artifact_id,
        cache_hit=structured_result.cache_hit,
        usage=structured_result.usage,
    )


def _load_selected_events(
    session_factory: sessionmaker[Session], event_ids: Sequence[int]
) -> tuple[NewsEvent, ...]:
    """Detach exactly the selected rows and source relationships needed to build dossiers."""
    expected_ids = _unique_event_ids(event_ids)
    if not expected_ids:
        return ()
    with UnitOfWork(session_factory) as unit:
        assert unit.session is not None
        statement = (
            select(NewsEvent)
            .where(NewsEvent.id.in_(expected_ids))
            .options(
                joinedload(NewsEvent.representative_article).joinedload(Article.source),
                selectinload(NewsEvent.articles).joinedload(Article.source),
            )
        )
        events = list(unit.session.scalars(statement).unique())
        if {event.id for event in events} != set(expected_ids):
            msg = "one or more selected NewsEvents do not exist"
            raise ValueError(msg)
        if any(event.status != NewsEventStatus.SELECTED for event in events):
            msg = "evidence dossiers require NewsEvents selected by the ranking checkpoint"
            raise ValueError(msg)
        return tuple(sorted(events, key=_selected_event_sort_key))


def _event_dossier(
    event: NewsEvent, *, max_sources_per_event: int, max_chars: int
) -> EvidenceDossier:
    """Project one selected event into representative-first, distinct-source-preferred evidence."""
    representative = event.representative_article
    selected_articles = _select_evidence_articles(event, max_sources_per_event)
    sources = tuple(_evidence_source(article, max_chars=max_chars) for article in selected_articles)
    if not sources:
        raise InsufficientEvidenceError()
    if (
        event.importance_score is None
        or event.relevance_score is None
        or event.confidence_score is None
        or event.selection_reason is None
    ):
        msg = "selected NewsEvent is missing persisted ranking provenance"
        raise ValueError(msg)
    summary_source = (
        event.summary
        or representative.summary
        or representative.content_text
        or representative.title
    )
    return EvidenceDossier(
        event_id=event.id,
        title=_bounded_text(event.title, 240),
        summary=_bounded_text(summary_source, 400),
        selection_reason=_bounded_text(event.selection_reason, 600),
        importance_score=event.importance_score,
        relevance_score=event.relevance_score,
        confidence_score=event.confidence_score,
        representative_article=sources[0],
        evidence_sources=sources,
    )


def _select_evidence_articles(event: NewsEvent, max_sources_per_event: int) -> tuple[Article, ...]:
    """Use the representative first, then distinct sources and stable quality sorting."""
    representative = event.representative_article
    others = sorted(
        (article for article in event.articles if article.id != representative.id),
        key=_evidence_article_sort_key,
    )
    selected = [representative]
    selected_source_ids = {representative.source_id}
    for article in others:
        if article.source_id not in selected_source_ids:
            selected.append(article)
            selected_source_ids.add(article.source_id)
        if len(selected) == max_sources_per_event:
            return tuple(selected)
    for article in others:
        if article.id not in {chosen.id for chosen in selected}:
            selected.append(article)
        if len(selected) == max_sources_per_event:
            break
    return tuple(selected)


def _evidence_article_sort_key(article: Article) -> tuple[int, int, float, int]:
    """Rank source candidates by source priority, available body length, recency, and article ID."""
    published = article.published_at
    timestamp = published.timestamp() if published is not None else float("-inf")
    body_length = len(" ".join((article.content_text or "").split()))
    return (-article.source.priority, -body_length, -timestamp, article.id)


def _evidence_source(article: Article, *, max_chars: int) -> EvidenceSource:
    """Return only the documented article metadata plus a normalized bounded excerpt."""
    return EvidenceSource(
        article_id=article.id,
        source_id=article.source_id,
        title=_bounded_text(article.title, 400),
        url=article.url,
        published_at=article.published_at,
        text_excerpt=_bounded_text(_normalized_evidence_text(article), max_chars),
    )


def _normalized_evidence_text(article: Article) -> str:
    """Prefer extracted body, then summary, then title for deterministic partial evidence."""
    return " ".join((article.content_text or article.summary or article.title).split())


def _fit_total_evidence_budget(
    dossiers: tuple[EvidenceDossier, ...],
    *,
    max_total_evidence_chars: int,
    min_publishable_events: int,
) -> tuple[EvidenceDossier, ...]:
    """Trim nonrepresentatives, then all sources, and drop lower-ranked events only if needed."""
    current = dossiers
    while _total_evidence_chars(current) > max_total_evidence_chars:
        current = _trim_nonrepresentatives(current, max_total_evidence_chars)
        if _total_evidence_chars(current) <= max_total_evidence_chars:
            return current
        current = _trim_all_proportionally(current, max_total_evidence_chars)
        if _total_evidence_chars(current) <= max_total_evidence_chars:
            return current
        if len(current) <= min_publishable_events:
            raise InsufficientEvidenceError()
        current = current[:-1]
    return current


def _trim_nonrepresentatives(
    dossiers: tuple[EvidenceDossier, ...], max_total_evidence_chars: int
) -> tuple[EvidenceDossier, ...]:
    """First remove excess characters from lower-ranked nonrepresentative excerpts only."""
    excess = _total_evidence_chars(dossiers) - max_total_evidence_chars
    if excess <= 0:
        return dossiers
    rewritten = [list(dossier.evidence_sources) for dossier in dossiers]
    for dossier_index in range(len(rewritten) - 1, -1, -1):
        for source_index in range(len(rewritten[dossier_index]) - 1, 0, -1):
            source = rewritten[dossier_index][source_index]
            removable = len(source.text_excerpt) - 1
            if removable <= 0:
                continue
            reduced_by = min(removable, excess)
            rewritten[dossier_index][source_index] = source.model_copy(
                update={
                    "text_excerpt": source.text_excerpt[: len(source.text_excerpt) - reduced_by]
                }
            )
            excess -= reduced_by
            if excess == 0:
                return _replace_dossier_sources(dossiers, rewritten)
    return _replace_dossier_sources(dossiers, rewritten)


def _trim_all_proportionally(
    dossiers: tuple[EvidenceDossier, ...], max_total_evidence_chars: int
) -> tuple[EvidenceDossier, ...]:
    """Then proportionally shorten remaining excerpts without exceeding the total cap."""
    sources = [source for dossier in dossiers for source in dossier.evidence_sources]
    if not sources or max_total_evidence_chars < len(sources):
        return dossiers
    total_chars = sum(len(source.text_excerpt) for source in sources)
    allocations = [
        max(1, (len(source.text_excerpt) * max_total_evidence_chars) // total_chars)
        for source in sources
    ]
    while sum(allocations) > max_total_evidence_chars:
        for index in range(len(allocations) - 1, -1, -1):
            if allocations[index] > 1 and sum(allocations) > max_total_evidence_chars:
                allocations[index] -= 1
    while sum(allocations) < max_total_evidence_chars:
        for index, source in enumerate(sources):
            if allocations[index] < len(source.text_excerpt):
                allocations[index] += 1
                if sum(allocations) == max_total_evidence_chars:
                    break
        else:
            break
    iterator = iter(allocations)
    rewritten = [
        [
            source.model_copy(update={"text_excerpt": source.text_excerpt[: next(iterator)]})
            for source in dossier.evidence_sources
        ]
        for dossier in dossiers
    ]
    return _replace_dossier_sources(dossiers, rewritten)


def _replace_dossier_sources(
    dossiers: Sequence[EvidenceDossier], sources: Sequence[Sequence[EvidenceSource]]
) -> tuple[EvidenceDossier, ...]:
    """Copy immutable Pydantic dossiers after deterministic excerpt trimming."""
    return tuple(
        dossier.model_copy(
            update={
                "representative_article": replacement[0],
                "evidence_sources": tuple(replacement),
            }
        )
        for dossier, replacement in zip(dossiers, sources, strict=True)
    )


def _outline_messages(
    dossiers: Sequence[EvidenceDossier],
    *,
    target_duration_seconds: int,
    duration_tolerance_seconds: int,
    max_outline_sections: int,
    prompt: PromptTemplate,
) -> tuple[LLMMessage, ...]:
    """Create the canonical bounded prompt payload without raw article text or secrets."""
    payload = {
        "constraints": {
            "target_duration_seconds": target_duration_seconds,
            "duration_tolerance_seconds": duration_tolerance_seconds,
            "max_outline_sections": max_outline_sections,
            "allowed_section_types": ["intro", "news", "transition", "outro"],
            "schema_version": "1",
        },
        "events": [dossier.model_dump(mode="json") for dossier in dossiers],
    }
    return (
        LLMMessage(role="system", content=prompt.system_instruction),
        LLMMessage(role="user", content=_canonical_json(payload)),
    )


def _selected_event_sort_key(event: NewsEvent) -> tuple[float, float, float, float, int]:
    """Keep output and drop decisions deterministic by using the code-owned ranking metrics."""
    return (
        -(event.importance_score or 0.0),
        -(event.relevance_score or 0.0),
        -(event.confidence_score or 0.0),
        -event.deterministic_score,
        event.id,
    )


def _total_evidence_chars(dossiers: Sequence[EvidenceDossier]) -> int:
    """Count precisely the excerpt characters included in the outbound model request."""
    return sum(
        len(source.text_excerpt) for dossier in dossiers for source in dossier.evidence_sources
    )


def _unique_event_ids(event_ids: Sequence[int]) -> tuple[int, ...]:
    """Reject duplicate or malformed caller identifiers before source loading or cache lookup."""
    normalized = tuple(event_ids)
    if not normalized or not all(
        isinstance(event_id, int) and event_id > 0 for event_id in normalized
    ):
        msg = "selected event IDs must be a non-empty sequence of positive integers"
        raise ValueError(msg)
    if len(normalized) != len(set(normalized)):
        msg = "selected event IDs must not repeat"
        raise ValueError(msg)
    return normalized


def _validate_evidence_limits(
    *,
    max_sources_per_event: int,
    max_chars_per_source: int,
    max_total_evidence_chars: int,
    min_publishable_events: int,
) -> None:
    """Keep all configured limits finite and aligned with the hard schema source ceiling."""
    if not 1 <= max_sources_per_event <= 3:
        msg = "max_sources_per_event must be between 1 and 3"
        raise ValueError(msg)
    if not 1 <= max_chars_per_source <= 1200:
        msg = "max_chars_per_source must be between 1 and 1200"
        raise ValueError(msg)
    if max_total_evidence_chars < 1 or min_publishable_events < 1:
        msg = "total evidence and minimum publishable-event limits must be positive"
        raise ValueError(msg)


def _validate_outline_limits(
    *,
    event_ids: tuple[int, ...],
    target_duration_seconds: int,
    duration_tolerance_seconds: int,
    max_outline_sections: int,
) -> None:
    """Validate configuration before it can affect artifact identity or model prompts."""
    if not event_ids:
        msg = "outline generation requires at least one selected event"
        raise ValueError(msg)
    if target_duration_seconds < 1 or duration_tolerance_seconds < 0:
        msg = "outline duration configuration is invalid"
        raise ValueError(msg)
    if not 3 <= max_outline_sections <= 12:
        msg = "max_outline_sections must be between 3 and 12"
        raise ValueError(msg)


def _bounded_text(value: str, limit: int) -> str:
    """Normalize whitespace and enforce a deterministic character cap."""
    return " ".join(value.split())[:limit]


def _canonical_json(value: object) -> str:
    """Serialize model input deterministically so identical bounded evidence hits the cache."""
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
