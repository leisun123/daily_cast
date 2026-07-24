"""LLM-backed event ranking with bounded cards and deterministic final selection."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload, sessionmaker

from dailycast.db.models import Article, LLMOperation, NewsEvent, NewsEventStatus
from dailycast.db.repositories import NewsEventRepository
from dailycast.db.transactions import UnitOfWork
from dailycast.llm.artifacts import LLMArtifactService
from dailycast.llm.budget import BudgetController
from dailycast.llm.contracts import JSONValue, LLMMessage, LLMProvider, LLMUsage
from dailycast.llm.outline_editorial import (
    EvidenceDossierBuildResult,
    OutlineGenerationResult,
)
from dailycast.llm.outline_editorial import (
    build_evidence_dossiers as build_outline_evidence_dossiers,
)
from dailycast.llm.outline_editorial import (
    generate_outline as generate_episode_outline,
)
from dailycast.llm.prompts import PromptTemplate
from dailycast.llm.prompts.generate_metadata_v1 import GENERATE_METADATA_V1
from dailycast.llm.prompts.generate_outline_v2 import GENERATE_OUTLINE_V2
from dailycast.llm.prompts.generate_script_v3 import GENERATE_SCRIPT_V3
from dailycast.llm.prompts.review_script_v1 import REVIEW_SCRIPT_V1
from dailycast.llm.prompts.revise_script_v1 import REVISE_SCRIPT_V1
from dailycast.llm.prompts.score_events_v2 import SCORE_EVENTS_V2
from dailycast.llm.schemas import (
    SCORE_EVENTS_V1_SCHEMA_VERSION,
    EventCard,
    EventScore,
    ScoreEventsV1,
)
from dailycast.llm.script_editorial import (
    ScriptGenerationResult,
)
from dailycast.llm.script_editorial import (
    generate_script as generate_episode_script,
)
from dailycast.llm.script_review_editorial import (
    MetadataGenerationResult,
    ScriptReviewResult,
    ScriptRevisionResult,
)
from dailycast.llm.script_review_editorial import (
    generate_metadata as generate_episode_metadata,
)
from dailycast.llm.script_review_editorial import (
    review_script as review_episode_script,
)
from dailycast.llm.script_review_editorial import (
    revise_script as revise_episode_script,
)
from dailycast.llm.script_schemas import ValidationReport
from dailycast.llm.script_validation import ScriptValidator

_MAX_SUMMARY_CHARS = 400
_MAX_EVIDENCE_CHARS = 240
_MAX_EVIDENCE_SNIPPETS = 2


@dataclass(frozen=True, slots=True)
class EventRankingResult:
    """A persisted ranking batch suitable for a pipeline checkpoint summary."""

    scored_event_ids: tuple[int, ...]
    selected_event_ids: tuple[int, ...]
    artifact_id: int | None
    cache_hit: bool
    usage: LLMUsage
    provider_call_count: int = 0


class AIEditorialService:
    """Score bounded NewsEvent cards and persist deterministic top-N selection decisions."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        provider: LLMProvider,
        *,
        max_candidates: int = 30,
        max_selected_events: int = 8,
        max_sources_per_event: int = 3,
        max_chars_per_source: int = 1200,
        max_total_evidence_chars: int = 24_000,
        min_publishable_events: int = 1,
        target_duration_seconds: int = 900,
        duration_tolerance_seconds: int = 60,
        max_outline_sections: int = 12,
        outline_prompt: PromptTemplate = GENERATE_OUTLINE_V2,
        script_prompt: PromptTemplate = GENERATE_SCRIPT_V3,
        estimated_chars_per_second: float = 4.0,
        script_duration_tolerance_ratio: float = 0.20,
        max_script_chars: int = 12_000,
        max_section_chars: int = 2_400,
        model_options: Mapping[str, JSONValue] | None = None,
    ) -> None:
        if max_candidates < 1 or max_selected_events < 1:
            msg = "editorial candidate and selected-event limits must be positive"
            raise ValueError(msg)
        self._session_factory = session_factory
        self._provider = provider
        self._max_candidates = max_candidates
        self._max_selected_events = max_selected_events
        self._max_sources_per_event = max_sources_per_event
        self._max_chars_per_source = max_chars_per_source
        self._max_total_evidence_chars = max_total_evidence_chars
        self._min_publishable_events = min_publishable_events
        self._target_duration_seconds = target_duration_seconds
        self._duration_tolerance_seconds = duration_tolerance_seconds
        self._max_outline_sections = max_outline_sections
        self._outline_prompt = outline_prompt
        self._script_prompt = script_prompt
        self._script_validator = ScriptValidator(
            estimated_chars_per_second=estimated_chars_per_second,
            duration_tolerance_ratio=script_duration_tolerance_ratio,
            max_script_chars=max_script_chars,
            max_section_chars=max_section_chars,
        )
        self._model_options = dict(model_options or {})

    def build_event_cards(self, event_ids: Sequence[int]) -> tuple[EventCard, ...]:
        """Load, deterministically pre-sort, and bound NewsEvents for a score-events request."""
        candidates = self._load_candidates(event_ids)
        return tuple(_event_card(event) for event in candidates)

    async def score_events(
        self,
        event_ids: Sequence[int],
        *,
        task_run_id: str,
        task_step_id: int,
        budget: BudgetController,
    ) -> EventRankingResult:
        """Score at most configured candidates, then select and persist the top N in normal code."""
        candidates = self._load_candidates(event_ids)
        if not candidates:
            return EventRankingResult((), (), None, False, LLMUsage())
        cards = tuple(_event_card(event) for event in candidates)
        event_card_ids = tuple(card.event_id for card in cards)
        messages = _score_messages(cards)
        artifact_service = LLMArtifactService(self._session_factory, self._provider, budget)
        structured_result = await artifact_service.generate_structured(
            operation=LLMOperation.SCORE_EVENTS,
            messages=messages,
            response_schema=ScoreEventsV1,
            prompt_version=SCORE_EVENTS_V2.version,
            schema_version=SCORE_EVENTS_V1_SCHEMA_VERSION,
            model_options=self._model_options,
            created_by_task_run_id=task_run_id,
            created_by_task_step_id=task_step_id,
            validation_context={"event_ids": event_card_ids},
        )
        score_batch = ScoreEventsV1.model_validate(
            structured_result.content,
            context={"event_ids": event_card_ids},
        )
        selected_event_ids = _select_event_ids(
            candidates, score_batch.scores, self._max_selected_events
        )
        self._persist_scores(
            candidates,
            score_batch.scores,
            selected_event_ids,
            model=structured_result.model,
        )
        return EventRankingResult(
            scored_event_ids=event_card_ids,
            selected_event_ids=selected_event_ids,
            artifact_id=structured_result.artifact_id,
            cache_hit=structured_result.cache_hit,
            usage=structured_result.usage,
            provider_call_count=structured_result.provider_call_count,
        )

    def build_evidence_dossiers(self, event_ids: Sequence[int]) -> EvidenceDossierBuildResult:
        """Build bounded selected-event EvidenceDossiers without exposing full article bodies."""
        return build_outline_evidence_dossiers(
            self._session_factory,
            event_ids,
            max_sources_per_event=self._max_sources_per_event,
            max_chars_per_source=self._max_chars_per_source,
            max_total_evidence_chars=self._max_total_evidence_chars,
            min_publishable_events=self._min_publishable_events,
        )

    async def generate_outline(
        self,
        selected_event_ids: Sequence[int],
        evidence_dossiers: Sequence[object],
        *,
        task_run_id: str,
        task_step_id: int,
        budget: BudgetController,
    ) -> OutlineGenerationResult:
        """Generate a cacheable EpisodeOutline from only validated bounded EvidenceDossiers."""
        from dailycast.llm.outline_schemas import EvidenceDossier

        dossiers = tuple(EvidenceDossier.model_validate(dossier) for dossier in evidence_dossiers)
        return await generate_episode_outline(
            self._session_factory,
            self._provider,
            selected_event_ids,
            dossiers,
            task_run_id=task_run_id,
            task_step_id=task_step_id,
            budget=budget,
            model_options=self._model_options,
            prompt=self._outline_prompt,
            target_duration_seconds=self._target_duration_seconds,
            duration_tolerance_seconds=self._duration_tolerance_seconds,
            max_outline_sections=self._max_outline_sections,
        )

    async def generate_script(
        self,
        outline: object,
        evidence_dossiers: Sequence[object],
        *,
        task_run_id: str,
        task_step_id: int,
        budget: BudgetController,
    ) -> ScriptGenerationResult:
        """Generate a bounded EpisodeScript through the existing Artifact cache and budget."""
        from dailycast.llm.outline_schemas import EpisodeOutline, EvidenceDossier

        validated_outline = EpisodeOutline.model_validate(outline)
        dossiers = tuple(EvidenceDossier.model_validate(dossier) for dossier in evidence_dossiers)
        return await generate_episode_script(
            self._session_factory,
            self._provider,
            validated_outline,
            dossiers,
            task_run_id=task_run_id,
            task_step_id=task_step_id,
            budget=budget,
            model_options=self._model_options,
            prompt=self._script_prompt,
        )

    def validate_script(
        self,
        script: object,
        outline: object,
        evidence_dossiers: Sequence[object],
    ) -> ValidationReport:
        """Run deterministic traceability, formatting, number, and duration checks on one script."""
        from dailycast.llm.outline_schemas import EpisodeOutline, EvidenceDossier
        from dailycast.llm.script_schemas import EpisodeScript

        validated_script = EpisodeScript.model_validate(script)
        validated_outline = EpisodeOutline.model_validate(outline)
        dossiers = tuple(EvidenceDossier.model_validate(dossier) for dossier in evidence_dossiers)
        return self._script_validator.validate(validated_script, validated_outline, dossiers)

    async def review_script(
        self,
        script: object,
        evidence_dossiers: Sequence[object],
        *,
        task_run_id: str,
        task_step_id: int,
        budget: BudgetController,
    ) -> ScriptReviewResult:
        """Run a cached review that can cite only supplied script evidence."""
        from dailycast.llm.outline_schemas import EvidenceDossier
        from dailycast.llm.script_schemas import EpisodeScript

        validated_script = EpisodeScript.model_validate(script)
        dossiers = tuple(EvidenceDossier.model_validate(dossier) for dossier in evidence_dossiers)
        return await review_episode_script(
            self._session_factory,
            self._provider,
            validated_script,
            dossiers,
            task_run_id=task_run_id,
            task_step_id=task_step_id,
            budget=budget,
            model_options=self._model_options,
            prompt=REVIEW_SCRIPT_V1,
        )

    async def revise_script(
        self,
        script: object,
        outline: object,
        evidence_dossiers: Sequence[object],
        validation_report: ValidationReport,
        review: object,
        *,
        task_run_id: str,
        task_step_id: int,
        budget: BudgetController,
    ) -> ScriptRevisionResult:
        """Generate a single constrained revision; callers enforce its attempt limit."""
        from dailycast.llm.outline_schemas import EpisodeOutline, EvidenceDossier
        from dailycast.llm.script_schemas import EpisodeScript, ScriptReview

        validated_script = EpisodeScript.model_validate(script)
        validated_outline = EpisodeOutline.model_validate(outline)
        validated_review = ScriptReview.model_validate(review)
        dossiers = tuple(EvidenceDossier.model_validate(dossier) for dossier in evidence_dossiers)
        return await revise_episode_script(
            self._session_factory,
            self._provider,
            validated_script,
            validated_outline,
            dossiers,
            validation_report,
            validated_review,
            task_run_id=task_run_id,
            task_step_id=task_step_id,
            budget=budget,
            model_options=self._model_options,
            prompt=REVISE_SCRIPT_V1,
        )

    async def generate_metadata(
        self,
        script: object,
        selected_event_titles: Sequence[str],
        *,
        estimated_duration_seconds: float,
        task_run_id: str,
        task_step_id: int,
        budget: BudgetController,
    ) -> MetadataGenerationResult:
        """Generate cached plain-text metadata from titles and final validated script only."""
        from dailycast.llm.script_schemas import EpisodeScript

        validated_script = EpisodeScript.model_validate(script)
        return await generate_episode_metadata(
            self._session_factory,
            self._provider,
            validated_script,
            tuple(selected_event_titles),
            estimated_duration_seconds=estimated_duration_seconds,
            task_run_id=task_run_id,
            task_step_id=task_step_id,
            budget=budget,
            model_options=self._model_options,
            prompt=GENERATE_METADATA_V1,
        )

    def _load_candidates(self, event_ids: Sequence[int]) -> tuple[NewsEvent, ...]:
        """Detach a bounded candidate set with only relationships needed to construct EventCards."""
        unique_ids = tuple(sorted(set(event_ids)))
        if not unique_ids:
            return ()
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            statement = (
                select(NewsEvent)
                .where(NewsEvent.id.in_(unique_ids))
                .options(
                    joinedload(NewsEvent.representative_article).joinedload(Article.source),
                    selectinload(NewsEvent.articles).joinedload(Article.source),
                )
            )
            events = list(unit.session.scalars(statement).unique())
            found_ids = {event.id for event in events}
            if found_ids != set(unique_ids):
                msg = "one or more ranking candidate NewsEvents do not exist"
                raise ValueError(msg)
            return tuple(sorted(events, key=_candidate_sort_key)[: self._max_candidates])

    def _persist_scores(
        self,
        candidates: Sequence[NewsEvent],
        scores: Sequence[EventScore],
        selected_event_ids: tuple[int, ...],
        *,
        model: str,
    ) -> None:
        """Persist model outputs and code-owned selection statuses in one short transaction."""
        scores_by_event_id = {score.event_id: score for score in scores}
        selected_ids = set(selected_event_ids)
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            repository = NewsEventRepository(unit.session)
            for candidate in candidates:
                event = repository.get(candidate.id)
                if event is None:
                    msg = f"NewsEvent {candidate.id} disappeared before score persistence"
                    raise RuntimeError(msg)
                score = scores_by_event_id[event.id]
                is_selected = event.id in selected_ids
                reason = score.reason if is_selected else f"Not selected: {score.reason}"
                repository.update(
                    event,
                    status=(NewsEventStatus.SELECTED if is_selected else NewsEventStatus.REJECTED),
                    importance_score=score.importance,
                    relevance_score=score.relevance,
                    confidence_score=score.confidence,
                    selection_reason=reason,
                    risk_flags_json=_canonical_json(score.risks),
                    score_json=_canonical_json(score.model_dump(mode="json")),
                    llm_model=model,
                    llm_prompt_version=SCORE_EVENTS_V2.version,
                )


def _score_messages(cards: Sequence[EventCard]) -> tuple[LLMMessage, ...]:
    """Create a bounded canonical EventCard payload with no full text or HTML."""
    payload = {"events": [card.model_dump(mode="json") for card in cards]}
    return (
        LLMMessage(role="system", content=SCORE_EVENTS_V2.system_instruction),
        LLMMessage(role="user", content=_canonical_json(payload)),
    )


def _event_card(event: NewsEvent) -> EventCard:
    """Project one event and bounded snippets without copying its full article body."""
    representative = event.representative_article
    articles = tuple(sorted(event.articles, key=_article_sort_key))
    source_priority = max((article.source.priority for article in articles), default=0)
    summary_source = event.summary or representative.summary or representative.content_text or ""
    return EventCard(
        event_id=event.id,
        title=_bounded_text(event.title, 240),
        summary=_bounded_text(summary_source, _MAX_SUMMARY_CHARS),
        source_count=event.source_count,
        source_priority=source_priority,
        published_time=event.last_published_at or event.first_published_at,
        representative_source=representative.source.name,
        evidence_snippets=_evidence_snippets(articles),
    )


def _candidate_sort_key(event: NewsEvent) -> tuple[float, int, int, float, int]:
    """Apply deterministic pre-ranking before the hard candidate cap and LLM call."""
    latest = event.last_published_at or event.first_published_at
    timestamp = latest.timestamp() if latest is not None else float("-inf")
    source_priority = max((article.source.priority for article in event.articles), default=0)
    return (
        -event.deterministic_score,
        -event.source_count,
        -source_priority,
        -timestamp,
        event.id,
    )


def _select_event_ids(
    candidates: Sequence[NewsEvent], scores: Sequence[EventScore], max_selected_events: int
) -> tuple[int, ...]:
    """Select a hard-capped number of events in normal Python, not from an LLM-selected count."""
    events_by_id = {event.id: event for event in candidates}
    ranked_scores = sorted(
        scores,
        key=lambda score: (
            -score.importance,
            -score.relevance,
            -score.confidence,
            -events_by_id[score.event_id].deterministic_score,
            score.event_id,
        ),
    )
    return tuple(score.event_id for score in ranked_scores[:max_selected_events])


def _article_sort_key(article: Article) -> tuple[int, int]:
    """Prefer stronger sources and stable IDs when selecting a maximum of two snippets."""
    return (-article.source.priority, article.id)


def _evidence_snippets(articles: Sequence[Article]) -> tuple[str, ...]:
    """Return no more than two non-empty, normalized short evidence excerpts."""
    snippets: list[str] = []
    for article in articles:
        source = article.summary or article.content_text or ""
        snippet = _bounded_text(source, _MAX_EVIDENCE_CHARS)
        if snippet and snippet not in snippets:
            snippets.append(snippet)
        if len(snippets) == _MAX_EVIDENCE_SNIPPETS:
            break
    return tuple(snippets)


def _bounded_text(value: str, limit: int) -> str:
    """Normalize whitespace and impose an explicit character limit before model input."""
    return " ".join(value.split())[:limit]


def _canonical_json(value: object) -> str:
    """Persist stable structured outputs and construct deterministic model input text."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
