"""Validated shapes for the briefing evidence, LLM output, and renderer input."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_BRIEFING_ITEMS = 5


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

    headline: str = Field(min_length=1, max_length=28)
    summary: str = Field(min_length=1, max_length=110)
    why_it_matters: str = Field(min_length=1, max_length=55)
    source_name: str = Field(min_length=1)
    source_url: str = Field(min_length=1)

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

    overview: str = Field(min_length=1, max_length=120)
    items: list[BriefingItem] = Field(max_length=MAX_BRIEFING_ITEMS)
