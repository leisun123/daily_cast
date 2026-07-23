"""Local TF-IDF character n-gram event clustering with deterministic graph components."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from math import log, sqrt

from dailycast.news.types import ArticleCluster, ProcessableArticle, ProcessingPolicy


def cluster_articles(
    articles: tuple[ProcessableArticle, ...], policy: ProcessingPolicy
) -> tuple[ArticleCluster, ...]:
    """Cluster eligible primary Articles by temporal, language, and TF-IDF similarity edges."""
    ordered = tuple(sorted(articles, key=lambda article: article.id))
    if not ordered:
        return ()
    vectors = _tfidf_vectors(ordered)
    components = _components(ordered, vectors, policy)
    clusters: list[ArticleCluster] = []
    by_id = {article.id: article for article in ordered}
    for component in components:
        clusters.extend(_apply_representative_guard(component, by_id, vectors, policy))
    return tuple(sorted(clusters, key=lambda cluster: cluster.article_ids[0]))


def _tfidf_vectors(
    articles: tuple[ProcessableArticle, ...],
) -> dict[int, dict[str, float]]:
    """Build local sparse character-trigram TF-IDF vectors from weighted editorial evidence."""
    term_counts = {article.id: Counter(_weighted_ngrams(article)) for article in articles}
    document_frequency: Counter[str] = Counter()
    for counts in term_counts.values():
        document_frequency.update(counts.keys())
    document_count = len(articles)
    vectors: dict[int, dict[str, float]] = {}
    for article_id, counts in term_counts.items():
        total_count = sum(counts.values())
        if total_count == 0:
            vectors[article_id] = {}
            continue
        vector = {
            gram: (count / total_count)
            * (log((1 + document_count) / (1 + document_frequency[gram])) + 1)
            for gram, count in counts.items()
        }
        magnitude = sqrt(sum(weight * weight for weight in vector.values()))
        vectors[article_id] = (
            {gram: weight / magnitude for gram, weight in vector.items()} if magnitude else {}
        )
    return vectors


def _components(
    articles: tuple[ProcessableArticle, ...],
    vectors: dict[int, dict[str, float]],
    policy: ProcessingPolicy,
) -> tuple[tuple[int, ...], ...]:
    """Build thresholded undirected edges then return sorted connected components."""
    union_find = _UnionFind(article.id for article in articles)
    for index, first in enumerate(articles):
        for second in articles[index + 1 :]:
            if not _can_cluster(first, second, policy):
                continue
            if _cosine(vectors[first.id], vectors[second.id]) >= policy.similarity_threshold:
                union_find.union(first.id, second.id)
    grouped: dict[int, list[int]] = {}
    for article in articles:
        grouped.setdefault(union_find.find(article.id), []).append(article.id)
    return tuple(tuple(sorted(member_ids)) for _, member_ids in sorted(grouped.items()))


def _apply_representative_guard(
    component: tuple[int, ...],
    by_id: dict[int, ProcessableArticle],
    vectors: dict[int, dict[str, float]],
    policy: ProcessingPolicy,
) -> tuple[ArticleCluster, ...]:
    """Avoid a similarity chain joining separate events without a representative evidence edge."""
    representative = min((by_id[article_id] for article_id in component), key=_quality_key)
    retained_ids = tuple(
        article_id
        for article_id in component
        if article_id == representative.id
        or _cosine(vectors[representative.id], vectors[article_id]) >= policy.similarity_threshold
    )
    excluded_ids = tuple(article_id for article_id in component if article_id not in retained_ids)
    clusters = [ArticleCluster(retained_ids, representative.id)]
    clusters.extend(ArticleCluster((article_id,), article_id) for article_id in excluded_ids)
    return tuple(clusters)


def _weighted_ngrams(article: ProcessableArticle) -> tuple[str, ...]:
    """Give title and summary more influence than the bounded leading article body."""
    title = _character_ngrams(article.title)
    summary = _character_ngrams(article.summary or "")
    content = _character_ngrams((article.content_text or "")[:4000])
    return title * 3 + summary * 2 + content


def _character_ngrams(value: str, *, n: int = 3) -> tuple[str, ...]:
    """Produce normalized character trigrams without requiring a language-specific tokenizer."""
    compact = " ".join(value.casefold().split())
    if len(compact) < n:
        return (compact,) if compact else ()
    return tuple(compact[index : index + n] for index in range(len(compact) - n + 1))


def _can_cluster(
    first: ProcessableArticle, second: ProcessableArticle, policy: ProcessingPolicy
) -> bool:
    """Keep graph edges inside the approved temporal and language buckets."""
    if (first.language or "").casefold() != (second.language or "").casefold():
        return False
    return abs(_effective_time(first) - _effective_time(second)) <= timedelta(
        hours=policy.cluster_time_window_hours
    )


def _effective_time(article: ProcessableArticle) -> datetime:
    """Use source publication time, falling back only to durable discovery time for clustering."""
    value = article.published_at or article.discovered_at
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _cosine(first: dict[str, float], second: dict[str, float]) -> float:
    """Compute sparse dot product after `_tfidf_vectors` has normalized both vectors."""
    smaller, larger = (first, second) if len(first) <= len(second) else (second, first)
    return sum(weight * larger.get(gram, 0.0) for gram, weight in smaller.items())


def _quality_key(article: ProcessableArticle) -> tuple[int, int, float, int]:
    """Order source priority, content completeness, publication recency, then durable ID."""
    return (
        -article.source_priority,
        -len(article.content_text or ""),
        -_effective_time(article).timestamp(),
        article.id,
    )


class _UnionFind:
    """Small deterministic union-find implementation for V1 article-scale graph components."""

    def __init__(self, values: Iterable[int]) -> None:
        self._parent = {value: value for value in values}

    def find(self, value: int) -> int:
        """Return the stable root for one article ID with path compression."""
        parent = self._parent[value]
        if parent != value:
            self._parent[value] = self.find(parent)
        return self._parent[value]

    def union(self, first: int, second: int) -> None:
        """Join two components by their smaller root so root selection stays deterministic."""
        first_root = self.find(first)
        second_root = self.find(second)
        if first_root != second_root:
            self._parent[max(first_root, second_root)] = min(first_root, second_root)
