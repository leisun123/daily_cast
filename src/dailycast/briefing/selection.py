"""Literal, deterministic evidence selection for management-focused briefings."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from dailycast.briefing.schemas import BriefingEvidence

_ASCII_TOKEN = re.compile(r"^[a-z0-9_]+$", re.IGNORECASE)


class SelectionRule(BaseModel):
    """One literal rule with an explicit tier, specificity, and editorial reason."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    tier: str = Field(min_length=1)
    specificity: int = Field(ge=0)
    max_items_per_briefing: int | None = Field(default=None, ge=1)
    all_groups: tuple[tuple[str, ...], ...] = Field(min_length=1)
    none_of: tuple[str, ...] = ()
    reason: str = Field(min_length=1)

    @field_validator("all_groups")
    @classmethod
    def validate_match_groups(
        cls, value: tuple[tuple[str, ...], ...]
    ) -> tuple[tuple[str, ...], ...]:
        """Prevent an empty OR group from turning an AND rule into a universal match."""
        if any(not group or any(not term.strip() for term in group) for group in value):
            raise ValueError("all_groups must contain non-empty literal terms")
        return value

    @field_validator("none_of")
    @classmethod
    def validate_exclusions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Keep every exclusion literal intentional and matchable."""
        if any(not term.strip() for term in value):
            raise ValueError("none_of must contain non-empty literal terms")
        return value


class CategorySelectionPolicy(BaseModel):
    """Validated rules, exclusions, and optional fallback for one briefing category."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tiers: tuple[str, ...] = Field(min_length=1)
    editorial_selection: bool = False
    editorial_candidate_limit: int = Field(default=20, ge=1, le=50)
    editorial_max_candidates_per_source: int = Field(default=5, ge=1, le=20)
    diversify_first_tiers: tuple[str, ...] = ()
    max_items_per_publisher: int = Field(default=1, ge=1)
    fallback_max_items_per_publisher: int | None = Field(default=None, ge=1)
    rules: tuple[SelectionRule, ...] = ()
    required_any_of: tuple[str, ...] = ()
    fallback_any_of: tuple[str, ...] = ()
    fallback_tier: str | None = None
    global_excludes: tuple[str, ...] = ()
    paper_only_terms: tuple[str, ...] = ()

    @field_validator("tiers")
    @classmethod
    def validate_tiers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Ensure the YAML declares a complete, deterministic ordering."""
        if any(not tier.strip() for tier in value) or len(set(value)) != len(value):
            raise ValueError("tiers must be distinct non-empty values")
        return value

    @model_validator(mode="after")
    def validate_rule_tiers(self) -> CategorySelectionPolicy:
        """Reject an unorderable rule or a fallback whose tier is not declared."""
        declared = set(self.tiers)
        unknown_diversity_tiers = set(self.diversify_first_tiers) - declared
        if unknown_diversity_tiers:
            raise ValueError(
                f"unknown diversity tier: {', '.join(sorted(unknown_diversity_tiers))}"
            )
        unknown = {rule.tier for rule in self.rules} - declared
        if unknown:
            raise ValueError(f"unknown tier: {', '.join(sorted(unknown))}")
        if self.fallback_tier is not None and self.fallback_tier not in declared:
            raise ValueError(f"unknown fallback tier: {self.fallback_tier}")
        if self.fallback_any_of and self.fallback_tier is None:
            raise ValueError("fallback_tier is required when fallback_any_of is configured")
        if not self.fallback_any_of and self.fallback_tier is not None:
            raise ValueError("fallback_tier requires fallback_any_of")
        if (
            self.fallback_max_items_per_publisher is not None
            and self.fallback_max_items_per_publisher < self.max_items_per_publisher
        ):
            raise ValueError("fallback publisher limit cannot be lower than the primary limit")
        return self


class BriefingSelectionPolicy(BaseModel):
    """The only supported categories are the independently delivered telecom and AI reports."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    categories: dict[str, CategorySelectionPolicy]

    @model_validator(mode="after")
    def validate_categories(self) -> BriefingSelectionPolicy:
        """Avoid a typo silently disabling one daily leadership briefing."""
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
    """One verified, detached article that can safely be ranked in memory."""

    article_id: int
    source_id: str
    source_priority: int
    discovered_at: datetime
    evidence: BriefingEvidence


@dataclass(frozen=True, slots=True)
class RankedBriefingEvidence:
    """Fixed-order evidence plus the rule result exposed to the generation prompt."""

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
    """Load a separate policy file and fail fast on malformed or unknown structure."""
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
    """Select a bounded evidence set using configured local or editorial policy."""
    if limit < 1:
        return ()
    category_policy = policy.category(category)
    if category_policy.editorial_selection:
        return _editorial_candidate_pool(candidates, category_policy, limit=limit)
    ranked = [
        item
        for candidate in candidates
        if (item := _classify_candidate(candidate, category_policy)) is not None
    ]
    selected = _select_with_publisher_cap(
        ranked,
        category_policy,
        limit=limit,
        publisher_cap=category_policy.max_items_per_publisher,
    )
    fallback_cap = category_policy.fallback_max_items_per_publisher
    if (
        len(selected) < limit
        and fallback_cap is not None
        and fallback_cap > category_policy.max_items_per_publisher
    ):
        return _select_with_publisher_cap(
            ranked,
            category_policy,
            limit=limit,
            publisher_cap=fallback_cap,
        )
    return selected


def _editorial_candidate_pool(
    candidates: Sequence[BriefingSelectionCandidate],
    policy: CategorySelectionPolicy,
    *,
    limit: int,
) -> tuple[RankedBriefingEvidence, ...]:
    """Give the editor a bounded, source-balanced pool without topical keyword gates."""
    queues: dict[str, list[BriefingSelectionCandidate]] = {}
    for candidate in sorted(candidates, key=_candidate_source_recency_key):
        queue = queues.setdefault(candidate.source_id, [])
        if len(queue) < policy.editorial_max_candidates_per_source:
            queue.append(candidate)
    source_ids = list(queues)
    prepared: list[RankedBriefingEvidence] = []
    pool_limit = min(limit, policy.editorial_candidate_limit)
    while queues and len(prepared) < pool_limit:
        for source_id in source_ids[:]:
            current_queue = queues.get(source_id)
            if not current_queue:
                queues.pop(source_id, None)
                source_ids.remove(source_id)
                continue
            candidate = current_queue.pop(0)
            prepared.append(
                _ranked(
                    candidate,
                    tier="LLM",
                    specificity=0,
                    reason="已通过时间、正文和原文链接核验，交由编辑模型判断管理价值",
                    rule_id="editorial-llm",
                )
            )
            if len(prepared) == pool_limit:
                break
    return tuple(prepared)


def _select_with_publisher_cap(
    ranked: Sequence[RankedBriefingEvidence],
    category_policy: CategorySelectionPolicy,
    *,
    limit: int,
    publisher_cap: int,
) -> tuple[RankedBriefingEvidence, ...]:
    """Retain tier order while applying one explicit per-publisher ceiling."""
    selected: list[RankedBriefingEvidence] = []
    selected_article_ids: set[int] = set()
    publisher_counts: dict[str, int] = {}
    rule_counts: dict[str, int] = {}
    rule_limits = {rule.id: rule.max_items_per_briefing for rule in category_policy.rules}
    for tier in category_policy.tiers:
        tier_entries = [item for item in ranked if item.tier == tier]
        if tier in category_policy.diversify_first_tiers:
            for rule_id in _rule_ids_in_priority_order(tier_entries):
                for item in _interleave_same_bucket(
                    [entry for entry in tier_entries if entry.rule_id == rule_id]
                ):
                    if not _publisher_capacity_available(
                        item, publisher_counts, publisher_cap
                    ) or not _rule_capacity_available(item, rule_counts, rule_limits):
                        continue
                    selected.append(item)
                    selected_article_ids.add(item.article_id)
                    _record_publisher(item, publisher_counts)
                    _record_rule(item, rule_counts)
                    break
                if len(selected) == limit:
                    return tuple(selected)
        for specificity in sorted({item.specificity for item in tier_entries}, reverse=True):
            bucket = [item for item in tier_entries if item.specificity == specificity]
            for item in _interleave_same_bucket(bucket):
                if item.article_id in selected_article_ids:
                    continue
                if not _publisher_capacity_available(
                    item, publisher_counts, publisher_cap
                ) or not _rule_capacity_available(item, rule_counts, rule_limits):
                    continue
                selected.append(item)
                selected_article_ids.add(item.article_id)
                _record_publisher(item, publisher_counts)
                _record_rule(item, rule_counts)
                if len(selected) == limit:
                    return tuple(selected)
    return tuple(selected)


def _classify_candidate(
    candidate: BriefingSelectionCandidate,
    policy: CategorySelectionPolicy,
) -> RankedBriefingEvidence | None:
    """Choose the strongest literal rule, applying global exclusions before all positives."""
    text = f"{candidate.evidence.title}\n{candidate.evidence.excerpt}"
    if _matches_any(text, policy.global_excludes):
        return None
    if policy.required_any_of and not _matches_any(text, policy.required_any_of):
        return None
    tier_order = {tier: index for index, tier in enumerate(policy.tiers)}
    matching_rules = [rule for rule in policy.rules if _rule_matches(text, rule)]
    if matching_rules:
        rule = min(
            matching_rules,
            key=lambda item: (tier_order[item.tier], -item.specificity, item.id),
        )
        return _ranked(
            candidate,
            tier=rule.tier,
            specificity=rule.specificity,
            reason=rule.reason,
            rule_id=rule.id,
        )
    if _matches_any(text, policy.paper_only_terms):
        return None
    if policy.fallback_tier is not None and _matches_any(text, policy.fallback_any_of):
        return _ranked(
            candidate,
            tier=policy.fallback_tier,
            specificity=0,
            reason="通信产业兜底关注",
            rule_id=f"fallback-{policy.fallback_tier}",
        )
    return None


def _ranked(
    candidate: BriefingSelectionCandidate,
    *,
    tier: str,
    specificity: int,
    reason: str,
    rule_id: str,
) -> RankedBriefingEvidence:
    """Attach deterministic rule output while retaining the source-ordering inputs."""
    return RankedBriefingEvidence(
        evidence=candidate.evidence,
        tier=tier,
        specificity=specificity,
        reason=reason,
        rule_id=rule_id,
        source_id=candidate.source_id,
        source_priority=candidate.source_priority,
        discovered_at=candidate.discovered_at,
        article_id=candidate.article_id,
    )


def _rule_matches(text: str, rule: SelectionRule) -> bool:
    """Require one literal from every group and reject a matching local exclusion."""
    return all(_matches_any(text, group) for group in rule.all_groups) and not _matches_any(
        text, rule.none_of
    )


def _matches_any(text: str, terms: Sequence[str]) -> bool:
    """Evaluate a finite list of literals with the documented Chinese/Latin semantics."""
    return any(_matches_term(text, term) for term in terms)


def _matches_term(text: str, term: str) -> bool:
    """Match Chinese substrings, but require token boundaries around ASCII terms such as RAN."""
    normalized_text = unicodedata.normalize("NFKC", text).casefold()
    normalized_term = unicodedata.normalize("NFKC", term).casefold()
    if _ASCII_TOKEN.fullmatch(normalized_term):
        pattern = rf"(?<![a-z0-9_]){re.escape(normalized_term)}(?![a-z0-9_])"
        return re.search(pattern, normalized_text) is not None
    return normalized_term in normalized_text


def _interleave_same_bucket(
    entries: Sequence[RankedBriefingEvidence],
) -> tuple[RankedBriefingEvidence, ...]:
    """Rotate only equal tier/specificity records; broader priority is already resolved."""
    queues: dict[str, list[RankedBriefingEvidence]] = {}
    for entry in sorted(entries, key=_source_recency_key):
        queues.setdefault(entry.source_id, []).append(entry)
    rotation = list(queues.values())
    selected: list[RankedBriefingEvidence] = []
    while rotation:
        for queue in rotation[:]:
            selected.append(queue.pop(0))
            if not queue:
                rotation.remove(queue)
    return tuple(selected)


def _rule_ids_in_priority_order(entries: Sequence[RankedBriefingEvidence]) -> tuple[str, ...]:
    """Return one deterministic first-pass slot per matching management subtopic."""
    rule_priority = {item.rule_id: item.specificity for item in entries}
    return tuple(sorted(rule_priority, key=lambda rule_id: (-rule_priority[rule_id], rule_id)))


def _publisher_capacity_available(
    item: RankedBriefingEvidence, publisher_counts: dict[str, int], publisher_cap: int
) -> bool:
    """Keep every pass subject to the same cross-feed publisher ceiling."""
    return publisher_counts.get(publisher_key(item.evidence.source_url), 0) < publisher_cap


def _record_publisher(item: RankedBriefingEvidence, publisher_counts: dict[str, int]) -> None:
    """Count a selected item against the normalized outlet domain."""
    key = publisher_key(item.evidence.source_url)
    publisher_counts[key] = publisher_counts.get(key, 0) + 1


def _rule_capacity_available(
    item: RankedBriefingEvidence,
    rule_counts: dict[str, int],
    rule_limits: dict[str, int | None],
) -> bool:
    """Apply an explicit per-rule ceiling without weakening tier ordering."""
    limit = rule_limits.get(item.rule_id)
    return limit is None or rule_counts.get(item.rule_id, 0) < limit


def _record_rule(item: RankedBriefingEvidence, rule_counts: dict[str, int]) -> None:
    """Track a selected item against its literal policy rule."""
    rule_counts[item.rule_id] = rule_counts.get(item.rule_id, 0) + 1


def publisher_key(url: str) -> str:
    """Treat all feeds resolving to one outlet domain as one briefing publisher."""
    hostname = urlsplit(url).hostname
    if hostname is None:
        return url.casefold()
    return hostname.casefold().removeprefix("www.")


def _source_recency_key(entry: RankedBriefingEvidence) -> tuple[int, float, int]:
    """Retain the existing source priority, publication recency, then article ID ordering."""
    return (
        -entry.source_priority,
        -_utc_timestamp(entry.evidence.published_at or entry.discovered_at),
        entry.article_id,
    )


def _candidate_source_recency_key(
    candidate: BriefingSelectionCandidate,
) -> tuple[int, float, int]:
    """Prefer trusted and fresh sources while retaining every topical decision for the LLM."""
    return (
        -candidate.source_priority,
        -_utc_timestamp(candidate.evidence.published_at or candidate.discovered_at),
        candidate.article_id,
    )


def _utc_timestamp(value: datetime) -> float:
    """Normalize SQLite-like naïve values before source-local ordering."""
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.timestamp()
