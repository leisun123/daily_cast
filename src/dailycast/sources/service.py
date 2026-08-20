"""Application services that persist discovered and extracted articles through repositories."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from dailycast.core.hashes import sha256_text
from dailycast.core.time import Clock
from dailycast.db.models import Article, ArticleStatus, Source, SourceKind
from dailycast.db.repositories import ArticleRepository, SourceRepository
from dailycast.db.transactions import UnitOfWork
from dailycast.news.normalization import (
    normalize_content as _normalize_content,
)
from dailycast.news.normalization import (
    normalize_title as _normalize_title,
)
from dailycast.news.normalization import (
    normalize_url as _normalize_url,
)
from dailycast.sources.contracts import (
    ArticleCandidate,
    CollectionResult,
    CollectionWindow,
    ExtractedArticle,
    SourceCollector,
    SourceError,
)


class ArticleValidationError(ValueError):
    """A discovered candidate cannot be assigned a safe normalized Article identity."""

    def __init__(self, error: SourceError) -> None:
        super().__init__(error.summary)
        self.error = error


@dataclass(frozen=True, slots=True)
class ExtractionTarget:
    """The minimum detached Article and Source data required for an HTTP extraction attempt."""

    article_id: int
    url: str
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class CollectionPersistenceResult:
    """A bounded aggregate returned to the collecting pipeline checkpoint."""

    article_ids: tuple[int, ...]
    source_count: int
    successful_source_count: int
    warning_count: int


class ArticleService:
    """Normalize and persist candidates/extractions without exposing SQLAlchemy to collectors."""

    def __init__(
        self, session_factory: sessionmaker[Session], *, clock: Clock | None = None
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or Clock()

    def upsert_candidate(self, candidate: ArticleCandidate) -> Article:
        """Persist one URL-identity candidate, merging later feed observations safely."""
        normalized_url = normalize_url(candidate.url)
        normalized_title = normalize_title(candidate.title)
        if not normalized_title:
            raise ArticleValidationError(
                SourceError(
                    "INVALID_ARTICLE_TITLE", "article title is empty after normalization", False
                )
            )
        normalized_content = (
            normalize_text(candidate.content_text) if candidate.content_text else None
        )
        now = self._clock.now()
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            articles = ArticleRepository(unit.session)
            url_hash = sha256_text(normalized_url)
            existing_by_external = (
                articles.get_by_source_external_id(candidate.source_id, candidate.external_id)
                if candidate.external_id is not None
                else None
            )
            if existing_by_external is not None and existing_by_external.url_hash != url_hash:
                raise ArticleValidationError(
                    SourceError(
                        "RSS_EXTERNAL_ID_URL_CONFLICT",
                        "RSS external_id was observed with a different normalized URL",
                        False,
                    )
                )
            existing = existing_by_external or articles.get_by_url_hash(url_hash)
            if existing is None:
                return articles.upsert(
                    source_id=candidate.source_id,
                    external_id=candidate.external_id,
                    url=candidate.url,
                    normalized_url=normalized_url,
                    url_hash=url_hash,
                    title=candidate.title.strip(),
                    normalized_title=normalized_title,
                    title_hash=sha256_text(normalized_title),
                    summary=_clean_optional(candidate.summary),
                    content_text=normalized_content,
                    content_hash=sha256_text(normalized_content) if normalized_content else None,
                    language=candidate.language,
                    published_at=candidate.published_at,
                    discovered_at=now,
                    extracted_at=now if normalized_content else None,
                    content_updated_at=now if normalized_content else None,
                    status=(
                        ArticleStatus.EXTRACTED if normalized_content else ArticleStatus.DISCOVERED
                    ),
                    metadata_json=_metadata_json(candidate.metadata),
                    created_at=now,
                    updated_at=now,
                )
            merged_content = normalized_content or existing.content_text
            content_changed = (
                normalized_content is not None and normalized_content != existing.content_text
            )
            status = ArticleStatus.EXTRACTED if merged_content else existing.status
            return articles.upsert(
                source_id=existing.source_id,
                external_id=existing.external_id or candidate.external_id,
                url=existing.url,
                normalized_url=normalized_url,
                url_hash=existing.url_hash,
                title=candidate.title.strip() or existing.title,
                normalized_title=normalized_title,
                title_hash=sha256_text(normalized_title),
                summary=_clean_optional(candidate.summary) or existing.summary,
                content_text=merged_content,
                content_hash=sha256_text(merged_content) if merged_content else None,
                language=candidate.language or existing.language,
                published_at=candidate.published_at or existing.published_at,
                discovered_at=existing.discovered_at,
                extracted_at=now if normalized_content else existing.extracted_at,
                content_updated_at=now if content_changed else existing.content_updated_at,
                status=status,
                error_code=None if merged_content else existing.error_code,
                error_summary=None if merged_content else existing.error_summary,
                metadata_json=_metadata_json(candidate.metadata),
            )

    def extraction_targets(self, article_ids: tuple[int, ...]) -> tuple[ExtractionTarget, ...]:
        """Return only this run's Article rows that still need a full-text extraction attempt."""
        if not article_ids:
            return ()
        targets: list[ExtractionTarget] = []
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            articles = ArticleRepository(unit.session)
            sources = SourceRepository(unit.session)
            for article in articles.list_by_ids(article_ids):
                if article.content_text:
                    continue
                if article.status not in {
                    ArticleStatus.DISCOVERED,
                    ArticleStatus.EXTRACTION_FAILED,
                }:
                    continue
                source = sources.get(article.source_id)
                if source is None:
                    continue
                targets.append(
                    ExtractionTarget(
                        article_id=article.id,
                        url=article.url,
                        timeout_seconds=float(source.request_timeout_seconds),
                    )
                )
        return tuple(targets)

    def apply_extraction(self, article_id: int, extracted: ExtractedArticle) -> Article:
        """Store a successful clean-text extraction in a short transaction."""
        if extracted.error is not None or extracted.content_text is None:
            msg = "apply_extraction requires a successful ExtractedArticle"
            raise ValueError(msg)
        content_text = normalize_text(extracted.content_text)
        now = self._clock.now()
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            articles = ArticleRepository(unit.session)
            article = articles.get(article_id)
            if article is None:
                msg = f"Article {article_id} no longer exists"
                raise LookupError(msg)
            return articles.update(
                article,
                content_text=content_text,
                content_hash=sha256_text(content_text),
                status=ArticleStatus.EXTRACTED,
                fetched_at=extracted.fetched_at or now,
                extracted_at=now,
                content_updated_at=now,
                http_status=extracted.http_status,
                error_code=None,
                error_summary=None,
            )

    def record_extraction_failure(self, article_id: int, extracted: ExtractedArticle) -> Article:
        """Keep one article-level extraction failure without rolling back successful siblings."""
        if extracted.error is None:
            msg = "record_extraction_failure requires an ExtractedArticle error"
            raise ValueError(msg)
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            articles = ArticleRepository(unit.session)
            article = articles.get(article_id)
            if article is None:
                msg = f"Article {article_id} no longer exists"
                raise LookupError(msg)
            return articles.update(
                article,
                status=ArticleStatus.EXTRACTION_FAILED,
                fetched_at=extracted.fetched_at or self._clock.now(),
                http_status=extracted.http_status,
                error_code=extracted.error.code,
                error_summary=extracted.error.summary[:1000],
            )


class SourceCollectionService:
    """Collect enabled Sources and persist candidates while isolating each source failure."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        collectors: Mapping[SourceKind, SourceCollector],
        article_service: ArticleService,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._collectors = collectors
        self._article_service = article_service
        self._clock = clock or Clock()

    async def collect_enabled_sources(
        self, window: CollectionWindow
    ) -> CollectionPersistenceResult:
        """Run each enabled source independently and return the persisted Article IDs."""
        return await self.collect_sources(self._enabled_sources(), window)

    async def collect_sources(
        self, sources: Sequence[Source], window: CollectionWindow
    ) -> CollectionPersistenceResult:
        """Collect the given sources independently and persist their candidates.

        This one per-source loop backs both the podcast pipeline (all enabled
        sources) and the briefing flow (briefing-tagged sources only), so both
        paths share identical persistence and failure-isolation semantics.
        """
        article_ids: list[int] = []
        warning_count = 0
        successful_source_count = 0
        for source in sources:
            result = await self._collect_source(source, window)
            if result.error is not None:
                warning_count += 1 + len(result.errors)
                self._record_source_error(source.id, result.error)
                continue
            successful_source_count += 1
            warning_count += len(result.errors)
            self._record_source_success(source.id)
            for candidate in result.candidates:
                try:
                    article_ids.append(self._article_service.upsert_candidate(candidate).id)
                except ArticleValidationError:
                    warning_count += 1
        return CollectionPersistenceResult(
            article_ids=tuple(dict.fromkeys(article_ids)),
            source_count=len(sources),
            successful_source_count=successful_source_count,
            warning_count=warning_count,
        )

    async def _collect_source(self, source: Source, window: CollectionWindow) -> CollectionResult:
        """Map an unsupported source type to a source-local warning without stopping the batch."""
        collector = self._collectors.get(source.kind)
        if collector is None:
            return CollectionResult(
                source_id=source.id,
                error=SourceError(
                    code="UNSUPPORTED_SOURCE_KIND",
                    summary=f"no collector is enabled for source kind {source.kind.value}",
                    retryable=False,
                ),
            )
        return await collector.collect(source, window)

    def _enabled_sources(self) -> tuple[Source, ...]:
        """Read the current database truth for enabled sources without retaining the Session."""
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            return tuple(
                source for source in SourceRepository(unit.session).list() if source.enabled
            )

    def _record_source_success(self, source_id: str) -> None:
        """Persist a source success marker after discovery has completed."""
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            sources = SourceRepository(unit.session)
            source = sources.get(source_id)
            if source is not None:
                sources.update(
                    source,
                    last_success_at=self._clock.now(),
                    last_error_code=None,
                    last_error_summary=None,
                )

    def _record_source_error(self, source_id: str, error: SourceError) -> None:
        """Persist a short source error summary without losing its historical Articles."""
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            sources = SourceRepository(unit.session)
            source = sources.get(source_id)
            if source is not None:
                sources.update(
                    source,
                    last_error_code=error.code,
                    last_error_summary=error.summary[:1000],
                )


def normalize_url(url: str) -> str:
    """Return the shared Article URL identity with source-layer validation errors."""
    try:
        return _normalize_url(url)
    except ValueError as error:
        raise ArticleValidationError(
            SourceError("INVALID_ARTICLE_URL", str(error), False)
        ) from error


def normalize_title(value: str) -> str:
    """Return the shared title identity used by ingestion and deterministic processing."""
    return _normalize_title(value)


def normalize_text(value: str) -> str:
    """Return the shared content identity used by ingestion and deterministic processing."""
    return _normalize_content(value)


def _clean_optional(value: str | None) -> str | None:
    """Normalize optional feed text while preserving a true absence as NULL."""
    if value is None:
        return None
    cleaned = normalize_text(value)
    return cleaned or None


def _metadata_json(metadata: Mapping[str, str]) -> str:
    """Persist bounded source metadata as canonical JSON for deterministic upserts."""
    return json.dumps(dict(metadata), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
