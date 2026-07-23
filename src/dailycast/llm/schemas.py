"""Bounded EventCard and structured response schemas for LLM editorial operations."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationInfo, model_validator

SCORE_EVENTS_V1_SCHEMA_VERSION = "score_events_v1"


class EventCard(BaseModel):
    """The complete, bounded event evidence allowed in a score-events model request."""

    model_config = ConfigDict(extra="forbid")

    event_id: int = Field(gt=0)
    title: str = Field(max_length=240)
    summary: str = Field(max_length=400)
    source_count: int = Field(ge=1)
    source_priority: int = Field(ge=0, le=100)
    published_time: datetime | None
    representative_source: str = Field(max_length=200)
    evidence_snippets: tuple[str, ...] = Field(max_length=2)


class EventScore(BaseModel):
    """One locally constrained score for exactly one EventCard identifier."""

    model_config = ConfigDict(extra="forbid")

    event_id: int = Field(gt=0)
    importance: float = Field(ge=0, le=100)
    relevance: float = Field(ge=0, le=100)
    confidence: float = Field(ge=0, le=100)
    recommend: StrictBool
    reason: str = Field(min_length=1, max_length=600)
    risks: list[str] = Field(default_factory=list, max_length=8)


class ScoreEventsV1(BaseModel):
    """The complete score-events response accepted for artifact caching and persistence."""

    model_config = ConfigDict(extra="forbid")

    scores: list[EventScore] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def require_exact_event_card_coverage(self, info: ValidationInfo) -> ScoreEventsV1:
        """Reject unknown, repeated, or omitted event IDs using the request allowlist context."""
        context = info.context
        if context is None or "event_ids" not in context:
            return self
        raw_event_ids = context["event_ids"]
        if not isinstance(raw_event_ids, tuple) or not all(
            isinstance(event_id, int) for event_id in raw_event_ids
        ):
            raise ValueError("score-events validation context must contain event IDs")
        expected_ids = set(raw_event_ids)
        scored_ids = [score.event_id for score in self.scores]
        if len(scored_ids) != len(set(scored_ids)):
            raise ValueError("score-events response contains duplicate event IDs")
        if set(scored_ids) != expected_ids:
            raise ValueError("score-events response must cover exactly the supplied event IDs")
        return self
