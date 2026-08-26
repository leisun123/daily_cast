"""Objective candidate-pool preparation for management-focused briefings."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from dailycast.briefing.schemas import BriefingEvidence


class CategorySelectionPolicy(BaseModel):
    """Validated, non-semantic controls for one LLM editorial candidate pool."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tiers: tuple[str, ...] = Field(min_length=1)
    editorial_selection: bool = False
    editorial_candidate_limit: int = Field(default=20, ge=1, le=50)
    editorial_max_candidates_per_publisher: int = Field(default=5, ge=1, le=20)

    @field_validator("tiers")
    @classmethod
    def validate_tiers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Ensure the YAML declares one non-empty editorial tier."""
        if any(not tier.strip() for tier in value) or len(set(value)) != len(value):
            raise ValueError("tiers must be distinct non-empty values")
        return value

    @model_validator(mode="after")
    def validate_editorial_pool(self) -> CategorySelectionPolicy:
        """Prevent future config from reintroducing local semantic selection."""
        if not self.editorial_selection:
            raise ValueError("editorial_selection must be true for every briefing category")
        if self.tiers != ("LLM",):
            raise ValueError("tiers must be exactly [LLM] for every briefing category")
        return self


class BriefingSelectionPolicy(BaseModel):
    """The two independently generated management briefing categories."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    categories: dict[str, CategorySelectionPolicy]

    @model_validator(mode="after")
    def validate_categories(self) -> BriefingSelectionPolicy:
        """Avoid a typo silently disabling one daily management briefing."""
        required = {"telecom", "ai"}
        if set(self.categories) != required:
            raise ValueError(f"categories must be exactly: {', '.join(sorted(required))}")
        return self

    def category(self, name: str) -> CategorySelectionPolicy:
        """Return one explicit category policy without a default or implicit fallback."""
        try:
            return self.categories[name]
        except KeyError as error:
            raise ValueError(f"unknown briefing category: {name}") from error


@dataclass(frozen=True, slots=True)
class BriefingSelectionCandidate:
    """One verified, detached article that can safely enter an in-memory pool."""

    article_id: int
    source_id: str
    source_priority: int
    discovered_at: datetime
    evidence: BriefingEvidence


@dataclass(frozen=True, slots=True)
class RankedBriefingEvidence:
    """Prepared evidence plus trace metadata for generation and final link audit."""

    evidence: BriefingEvidence
    tier: str
    specificity: int
    reason: str
    rule_id: str
    source_id: str
    source_priority: int
    discovered_at: datetime
    article_id: int


def load_selection_policy(path: Path) -> BriefingSelectionPolicy:
    """Load a separate policy file and fail fast on malformed structure."""
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("briefing selection policy must be a mapping")
    try:
        return BriefingSelectionPolicy.model_validate(payload)
    except ValueError as error:
        raise ValueError(f"invalid briefing selection policy: {error}") from error


def select_evidence(
    category: str,
    candidates: Sequence[BriefingSelectionCandidate],
    policy: BriefingSelectionPolicy,
    *,
    limit: int,
) -> tuple[RankedBriefingEvidence, ...]:
    """Prepare a bounded, source-balanced evidence pool for LLM editorial selection."""
    if limit < 1:
        return ()
    return _editorial_candidate_pool(candidates, policy.category(category), limit=limit)


def _editorial_candidate_pool(
    candidates: Sequence[BriefingSelectionCandidate],
    policy: CategorySelectionPolicy,
    *,
    limit: int,
) -> tuple[RankedBriefingEvidence, ...]:
    """Bound candidates by publisher and recency without semantic judgments."""
    queues: dict[str, list[BriefingSelectionCandidate]] = {}
    for candidate in sorted(candidates, key=_candidate_recency_key):
        publisher = publisher_key(candidate.evidence.source_url)
        queue = queues.setdefault(publisher, [])
        if len(queue) < policy.editorial_max_candidates_per_publisher:
            queue.append(candidate)
    publishers = list(queues)
    prepared: list[RankedBriefingEvidence] = []
    pool_limit = min(limit, policy.editorial_candidate_limit)
    while queues and len(prepared) < pool_limit:
        for publisher in publishers[:]:
            current_queue = queues.get(publisher)
            if not current_queue:
                queues.pop(publisher, None)
                publishers.remove(publisher)
                continue
            candidate = current_queue.pop(0)
            prepared.append(_ranked(candidate))
            if len(prepared) == pool_limit:
                break
    return tuple(prepared)


def _ranked(candidate: BriefingSelectionCandidate) -> RankedBriefingEvidence:
    """Attach a transparent marker that semantic selection belongs to the LLM."""
    return RankedBriefingEvidence(
        evidence=candidate.evidence,
        tier="LLM",
        specificity=0,
        reason="已通过时间、正文和原文链接核验，交由编辑模型判断管理价值",
        rule_id="editorial-llm",
        source_id=candidate.source_id,
        source_priority=candidate.source_priority,
        discovered_at=candidate.discovered_at,
        article_id=candidate.article_id,
    )


def publisher_key(url: str) -> str:
    """Treat all feeds resolving to one outlet domain as one briefing publisher."""
    hostname = urlsplit(url).hostname
    if hostname is None:
        return url.casefold()
    return hostname.casefold().removeprefix("www.")


def _candidate_recency_key(
    candidate: BriefingSelectionCandidate,
) -> tuple[float, int]:
    """Order the pool reproducibly by freshness, without rating publisher quality."""
    return (
        -_utc_timestamp(candidate.evidence.published_at or candidate.discovered_at),
        candidate.article_id,
    )


def _utc_timestamp(value: datetime) -> float:
    """Normalize SQLite-like naïve values before source-local ordering."""
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.timestamp()
