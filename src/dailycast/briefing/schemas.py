"""Validated shapes for the briefing evidence, LLM output, and renderer input."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_BRIEFING_ITEMS = 5


def _compact_complete_sentence(value: object, max_length: int) -> object:
    """Compact only at an existing sentence boundary; never invent a false ending."""
    if not isinstance(value, str) or len(value) <= max_length:
        return value
    bounded = value[:max_length]
    last_sentence_end = max((bounded.rfind(mark) for mark in "。！？!?"), default=-1)
    if last_sentence_end >= 0:
        return bounded[: last_sentence_end + 1].rstrip()
    return value


def _require_no_ellipsis(value: object) -> object:
    """Reject visibly unfinished model prose so the service falls back to source evidence."""
    if isinstance(value, str) and ("…" in value or "..." in value):
        raise ValueError("briefing prose must not contain an ellipsis")
    return value


@dataclass(frozen=True, slots=True)
class BriefingEvidence:
    """One collected article presented to the LLM as bounded briefing evidence."""

    title: str
    source_name: str
    published_at: datetime | None
    excerpt: str
    source_url: str


class BriefingItem(BaseModel):
    """One briefing entry whose link must trace back to collected evidence."""

    model_config = ConfigDict(extra="forbid")

    headline: str = Field(min_length=1, max_length=36)
    # Kept intentionally short because it appears before the compact WeCom
    # headline.  The editorial model chooses it from the article evidence; the
    # renderer never infers a tag from a title or source on its own.
    theme: str = Field(default="", max_length=12)
    # The preferred lengths are controlled by the generation prompt.  These are
    # deliberately roomy hard limits: a renderer may omit a whole over-budget
    # item, but must never cut a factual sentence in half to make it fit.
    summary: str = Field(min_length=1, max_length=2_000)
    why_it_matters: str = Field(min_length=1, max_length=1_000)
    source_name: str = Field(min_length=1)
    source_url: str = Field(min_length=1)

    @field_validator("headline", mode="before")
    @classmethod
    def trim_headline_for_delivery(cls, value: object) -> object:
        """Reject an incomplete headline instead of making it look like a title."""
        if not isinstance(value, str):
            return value
        headline = value.strip()
        if "…" in headline or "..." in headline or len(headline) > 36:
            raise ValueError("headline must be a complete sentence of at most 36 characters")
        return headline

    @field_validator("theme", mode="before")
    @classmethod
    def trim_theme_for_delivery(cls, value: object) -> object:
        """Keep an optional topic tag compact and free of renderer punctuation."""
        if not isinstance(value, str):
            return value
        return value.strip().strip("｜|")

    @field_validator("summary", mode="before")
    @classmethod
    def trim_summary_for_delivery(cls, value: object) -> object:
        """Keep factual prose complete when the webhook-safe summary limit is exceeded."""
        return _require_no_ellipsis(_compact_complete_sentence(value, 160))

    @field_validator("why_it_matters", mode="before")
    @classmethod
    def trim_impact_for_delivery(cls, value: object) -> object:
        """Prevent a verbose impact sentence from discarding an entire item."""
        return _require_no_ellipsis(_compact_complete_sentence(value, 80))

    @field_validator("source_url")
    @classmethod
    def require_http_url(cls, value: str) -> str:
        """Reject non-web links so the renderer never ships a fabricated scheme."""
        if not value.startswith(("http://", "https://")):
            raise ValueError("source_url must be an absolute HTTP(S) URL")
        return value


class BriefingResult(BaseModel):
    """The LLM-written part of one category briefing before deterministic rendering."""

    model_config = ConfigDict(extra="forbid")

    overview: str = Field(min_length=1, max_length=600)
    items: list[BriefingItem] = Field(max_length=MAX_BRIEFING_ITEMS)

    @field_validator("overview", mode="before")
    @classmethod
    def trim_overview_for_delivery(cls, value: object) -> object:
        """Keep the complete category eligible when the model over-explains its overview."""
        return _require_no_ellipsis(_compact_complete_sentence(value, 120))


class MergedBriefingFocus(BaseModel):
    """The one natural editorial lead shown above the merged title list."""

    model_config = ConfigDict(extra="forbid")

    focus: str = Field(min_length=8, max_length=96)

    @field_validator("focus", mode="before")
    @classmethod
    def require_complete_focus(cls, value: object) -> object:
        """Never render a visibly unfinished editorial lead."""
        if not isinstance(value, str):
            return value
        return _require_no_ellipsis(value.strip())
