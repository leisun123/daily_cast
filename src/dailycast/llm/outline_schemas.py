"""Bounded evidence-dossier and episode-outline schemas for the generate-outline operation."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

GENERATE_OUTLINE_V1_SCHEMA_VERSION = "generate_outline_v1"

_MAX_OUTLINE_SECTION_GOAL_CHARS = 400
_MAX_OUTLINE_KEY_FACT_CHARS = 360
_MAX_OUTLINE_KEY_FACTS = 8


class EvidenceSource(BaseModel):
    """The only article-level source material allowed in an outline-generation request."""

    model_config = ConfigDict(extra="forbid")

    article_id: int = Field(gt=0)
    source_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=400)
    url: str = Field(min_length=1, max_length=4096)
    published_at: datetime | None
    text_excerpt: str = Field(min_length=1, max_length=1200)


class EvidenceDossier(BaseModel):
    """A bounded selected-event package; it deliberately contains no raw article-body field."""

    model_config = ConfigDict(extra="forbid")

    event_id: int = Field(gt=0)
    title: str = Field(min_length=1, max_length=240)
    summary: str = Field(max_length=400)
    selection_reason: str = Field(min_length=1, max_length=600)
    importance_score: float = Field(ge=0, le=100)
    relevance_score: float = Field(ge=0, le=100)
    confidence_score: float = Field(ge=0, le=100)
    representative_article: EvidenceSource
    evidence_sources: tuple[EvidenceSource, ...] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def require_representative_first(self) -> EvidenceDossier:
        """Keep representative provenance explicit and first in every evidence package."""
        if self.evidence_sources[0].article_id != self.representative_article.article_id:
            raise ValueError("evidence dossier must place the representative article first")
        article_ids = [source.article_id for source in self.evidence_sources]
        if len(article_ids) != len(set(article_ids)):
            raise ValueError("evidence dossier must not repeat an article")
        return self


class EpisodeOutlineSection(BaseModel):
    """One bounded chronological section of a schema-validated episode outline."""

    model_config = ConfigDict(extra="forbid")

    section_id: str = Field(min_length=1, max_length=80)
    type: Literal["intro", "news", "transition", "outro"]
    event_ids: tuple[int, ...] = Field(max_length=30)
    goal: str = Field(min_length=1, max_length=_MAX_OUTLINE_SECTION_GOAL_CHARS)
    key_facts: tuple[
        Annotated[str, Field(min_length=1, max_length=_MAX_OUTLINE_KEY_FACT_CHARS)], ...
    ] = Field(max_length=_MAX_OUTLINE_KEY_FACTS)
    seconds: int = Field(gt=0, le=3600)

    @model_validator(mode="after")
    def require_type_appropriate_content(self) -> EpisodeOutlineSection:
        """News sections need evidence-backed events and facts; framing sections may omit both."""
        if len(self.event_ids) != len(set(self.event_ids)):
            raise ValueError("outline section must not repeat event IDs")
        if self.type == "news" and not self.event_ids:
            raise ValueError("news outline section must reference at least one event")
        if self.type == "news" and not self.key_facts:
            raise ValueError("news outline section must include at least one key fact")
        return self


class EpisodeOutline(BaseModel):
    """The full locally validated outline accepted for artifact caching and future checkpoints."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"]
    title_angle: str = Field(min_length=1, max_length=240)
    target_seconds: int = Field(gt=0, le=7200)
    sections: tuple[EpisodeOutlineSection, ...] = Field(min_length=3, max_length=12)

    @model_validator(mode="after")
    def require_selected_event_coverage_and_duration(self, info: ValidationInfo) -> EpisodeOutline:
        """Reject unknown IDs, missing selections, and duration drift using caller constraints."""
        section_ids = [section.section_id for section in self.sections]
        if len(section_ids) != len(set(section_ids)):
            raise ValueError("outline response contains duplicate section IDs")
        context = info.context
        if context is None:
            return self
        selected_event_ids = _context_int_tuple(context, "selected_event_ids")
        target_seconds = _context_positive_int(context, "target_duration_seconds")
        tolerance_seconds = _context_nonnegative_int(context, "duration_tolerance_seconds")
        max_sections = _context_positive_int(context, "max_outline_sections")
        if len(self.sections) > max_sections:
            raise ValueError("outline response exceeds the configured section limit")
        if abs(self.target_seconds - target_seconds) > tolerance_seconds:
            raise ValueError("outline target duration differs from the configured target")
        actual_seconds = sum(section.seconds for section in self.sections)
        if abs(actual_seconds - target_seconds) > tolerance_seconds:
            raise ValueError("outline section duration differs from the configured target")
        allowed_ids = set(selected_event_ids)
        referenced_ids = {event_id for section in self.sections for event_id in section.event_ids}
        if not referenced_ids.issubset(allowed_ids):
            raise ValueError("outline response contains an unknown event ID")
        news_ids = {
            event_id
            for section in self.sections
            if section.type == "news"
            for event_id in section.event_ids
        }
        if news_ids != allowed_ids:
            raise ValueError("outline response must cover every selected event in a news section")
        return self


def _context_int_tuple(context: object, key: str) -> tuple[int, ...]:
    """Read strict immutable identifier allowlists passed from the editorial service."""
    if not isinstance(context, dict):
        raise ValueError("outline validation context must be a mapping")
    value = context.get(key)
    if (
        not isinstance(value, tuple)
        or not value
        or not all(isinstance(item, int) for item in value)
    ):
        raise ValueError(f"outline validation context must contain {key}")
    return value


def _context_positive_int(context: object, key: str) -> int:
    """Read a required positive integer constraint from Pydantic validation context."""
    value = _context_value(context, key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"outline validation context must contain positive {key}")
    return value


def _context_nonnegative_int(context: object, key: str) -> int:
    """Read a required nonnegative integer tolerance from Pydantic validation context."""
    value = _context_value(context, key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"outline validation context must contain nonnegative {key}")
    return value


def _context_value(context: object, key: str) -> object:
    """Return one named value from the caller-owned validation context."""
    if not isinstance(context, dict):
        raise ValueError("outline validation context must be a mapping")
    return context.get(key)
