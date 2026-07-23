"""Exact and near Article deduplication without external services or model calls."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from dailycast.news.types import DeduplicationResult, ProcessableArticle, ProcessingPolicy

DUPLICATE_URL_HASH = "DUPLICATE_URL_HASH"
DUPLICATE_CONTENT_HASH = "DUPLICATE_CONTENT_HASH"
DUPLICATE_TITLE_HASH = "DUPLICATE_TITLE_HASH"
DUPLICATE_NEAR_CONTENT = "DUPLICATE_NEAR_CONTENT"


def deduplicate_articles(
    articles: tuple[ProcessableArticle, ...], policy: ProcessingPolicy
) -> DeduplicationResult:
    """Choose one quality winner per exact or near-duplicate group deterministically."""
    by_id = {article.id: article for article in articles}
    active_ids = set(by_id)
    duplicate_of_article_ids: dict[int, int] = {}
    reasons: dict[int, str] = {}
    simhashes = {
        article.id: simhash64(article.content_text)
        for article in articles
        if article.content_text is not None
    }

    _deduplicate_exact(
        by_id,
        active_ids,
        duplicate_of_article_ids,
        reasons,
        lambda article: article.url_hash,
        DUPLICATE_URL_HASH,
    )
    _deduplicate_exact(
        by_id,
        active_ids,
        duplicate_of_article_ids,
        reasons,
        lambda article: article.content_hash,
        DUPLICATE_CONTENT_HASH,
    )
    _deduplicate_titles(
        by_id,
        active_ids,
        duplicate_of_article_ids,
        reasons,
        policy,
    )
    _deduplicate_near(
        by_id,
        active_ids,
        duplicate_of_article_ids,
        reasons,
        simhashes,
        policy,
    )
    return DeduplicationResult(
        primary_article_ids=tuple(sorted(active_ids)),
        duplicate_of_article_ids=duplicate_of_article_ids,
        reasons=reasons,
        simhashes=simhashes,
    )


def simhash64(content: str | None) -> str:
    """Return a 64-bit SimHash hex digest over normalized character trigrams."""
    if content is None:
        return "0000000000000000"
    grams = _character_ngrams(content, n=3)
    if not grams:
        return "0000000000000000"
    weights = [0] * 64
    for gram in sorted(grams):
        value = int.from_bytes(sha256(gram.encode("utf-8")).digest()[:8], byteorder="big")
        for bit in range(64):
            weights[bit] += 1 if value & (1 << bit) else -1
    result = sum(1 << bit for bit, weight in enumerate(weights) if weight >= 0)
    return f"{result:016x}"


def jaccard_similarity(first: str, second: str, *, n: int = 3) -> float:
    """Return deterministic character n-gram Jaccard similarity for bounded local comparison."""
    first_grams = set(_character_ngrams(first, n=n))
    second_grams = set(_character_ngrams(second, n=n))
    if not first_grams or not second_grams:
        return 0.0
    return len(first_grams & second_grams) / len(first_grams | second_grams)


def hamming_distance(first: str, second: str) -> int:
    """Return Hamming distance between two 64-bit SimHash hexadecimal strings."""
    return (int(first, 16) ^ int(second, 16)).bit_count()


def _deduplicate_exact(
    by_id: dict[int, ProcessableArticle],
    active_ids: set[int],
    duplicate_of_article_ids: dict[int, int],
    reasons: dict[int, str],
    key_for: Callable[[ProcessableArticle], str | None],
    reason: str,
) -> None:
    """Resolve each non-empty exact-identity group with the documented quality ordering."""
    groups: dict[str, list[ProcessableArticle]] = defaultdict(list)
    for article_id in sorted(active_ids):
        article = by_id[article_id]
        key = key_for(article)
        if isinstance(key, str) and key:
            groups[key].append(article)
    for group in groups.values():
        if len(group) > 1:
            _mark_group_duplicates(group, active_ids, duplicate_of_article_ids, reasons, reason)


def _deduplicate_titles(
    by_id: dict[int, ProcessableArticle],
    active_ids: set[int],
    duplicate_of_article_ids: dict[int, int],
    reasons: dict[int, str],
    policy: ProcessingPolicy,
) -> None:
    """Use title equality only when publication timestamps are within the approved window."""
    groups: dict[str, list[ProcessableArticle]] = defaultdict(list)
    for article_id in sorted(active_ids):
        article = by_id[article_id]
        groups[article.title_hash].append(article)
    for group in groups.values():
        remaining = sorted(group, key=_quality_key)
        while remaining:
            winner = remaining.pop(0)
            duplicates = [
                article
                for article in remaining
                if _within_window(
                    winner.published_at,
                    article.published_at,
                    policy.title_duplicate_window_hours,
                )
            ]
            _mark_specific_duplicates(
                winner,
                duplicates,
                active_ids,
                duplicate_of_article_ids,
                reasons,
                DUPLICATE_TITLE_HASH,
            )
            duplicate_ids = {article.id for article in duplicates}
            remaining = [article for article in remaining if article.id not in duplicate_ids]


def _deduplicate_near(
    by_id: dict[int, ProcessableArticle],
    active_ids: set[int],
    duplicate_of_article_ids: dict[int, int],
    reasons: dict[int, str],
    simhashes: dict[int, str],
    policy: ProcessingPolicy,
) -> None:
    """Apply bounded same-language SimHash/Jaccard comparison to surviving primary articles."""
    remaining = sorted((by_id[article_id] for article_id in active_ids), key=_quality_key)
    for index, winner in enumerate(remaining):
        if winner.id not in active_ids:
            continue
        duplicates: list[ProcessableArticle] = []
        for candidate in remaining[index + 1 :]:
            if candidate.id not in active_ids or not _can_compare_near(winner, candidate, policy):
                continue
            assert winner.content_text is not None
            assert candidate.content_text is not None
            distance = hamming_distance(simhashes[winner.id], simhashes[candidate.id])
            similarity = jaccard_similarity(winner.content_text, candidate.content_text)
            if distance <= policy.near_duplicate_hamming_distance or (
                similarity >= policy.near_duplicate_jaccard_threshold
            ):
                duplicates.append(candidate)
        _mark_specific_duplicates(
            winner,
            duplicates,
            active_ids,
            duplicate_of_article_ids,
            reasons,
            DUPLICATE_NEAR_CONTENT,
        )


def _mark_group_duplicates(
    group: list[ProcessableArticle],
    active_ids: set[int],
    duplicate_of_article_ids: dict[int, int],
    reasons: dict[int, str],
    reason: str,
) -> None:
    """Pick the deterministic best Article then assign all other group members to it."""
    ordered = sorted(group, key=_quality_key)
    _mark_specific_duplicates(
        ordered[0], ordered[1:], active_ids, duplicate_of_article_ids, reasons, reason
    )


def _mark_specific_duplicates(
    winner: ProcessableArticle,
    duplicates: list[ProcessableArticle],
    active_ids: set[int],
    duplicate_of_article_ids: dict[int, int],
    reasons: dict[int, str],
    reason: str,
) -> None:
    """Persist one in-memory duplicate decision while retaining the best article as primary."""
    for duplicate in duplicates:
        if duplicate.id == winner.id or duplicate.id not in active_ids:
            continue
        active_ids.remove(duplicate.id)
        duplicate_of_article_ids[duplicate.id] = winner.id
        reasons[duplicate.id] = reason


def _quality_key(article: ProcessableArticle) -> tuple[int, int, float, int]:
    """Order strongest source, longest body, newest publication, then lowest ID first."""
    timestamp = _as_utc(article.published_at or article.discovered_at).timestamp()
    return (-article.source_priority, -len(article.content_text or ""), -timestamp, article.id)


def _can_compare_near(
    first: ProcessableArticle, second: ProcessableArticle, policy: ProcessingPolicy
) -> bool:
    """Limit near comparison to same-language, nearby, non-empty article bodies."""
    if first.content_text is None or second.content_text is None:
        return False
    if (first.language or "").casefold() != (second.language or "").casefold():
        return False
    return _within_window(
        first.published_at,
        second.published_at,
        policy.near_duplicate_window_hours,
    )


def _within_window(first: datetime | None, second: datetime | None, hours: int) -> bool:
    """Require both publication times and keep comparisons inside a symmetric time window."""
    if first is None or second is None:
        return False
    return abs(_as_utc(first) - _as_utc(second)) <= timedelta(hours=hours)


def _as_utc(value: datetime) -> datetime:
    """Normalize SQLite-returned naive timestamps before comparing their time distance."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _character_ngrams(value: str, *, n: int) -> tuple[str, ...]:
    """Build a deterministic non-empty character n-gram sequence without a tokenizer."""
    compact = " ".join(value.casefold().split())
    if len(compact) < n:
        return (compact,) if compact else ()
    return tuple(compact[index : index + n] for index in range(len(compact) - n + 1))
