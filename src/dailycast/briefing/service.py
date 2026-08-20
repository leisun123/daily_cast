"""Independent daily text briefing flow: collect, filter, generate, render, push."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session, sessionmaker

from dailycast.briefing.prompt import build_briefing_messages
from dailycast.briefing.renderer import render_briefing, truncate_markdown
from dailycast.briefing.schemas import BriefingEvidence, BriefingResult
from dailycast.briefing.wecom import WeComNotifier
from dailycast.core.time import Clock
from dailycast.db.models import LLMOperation, Source
from dailycast.db.repositories import ArticleRepository, SourceRepository
from dailycast.db.transactions import UnitOfWork
from dailycast.llm.contracts import LLMProvider
from dailycast.news.service import NewsProcessor
from dailycast.sources.contracts import CollectionWindow
from dailycast.sources.extraction import ContentExtractor, FetchPolicy
from dailycast.sources.service import ArticleService, SourceCollectionService

logger = logging.getLogger(__name__)

CATEGORY_TITLES: dict[str, str] = {
    "telecom": "通信行业日报",
    "ai": "AI 动态日报",
}


@dataclass(frozen=True, slots=True)
class BriefingCategoryReport:
    """The per-category outcome of one briefing run."""

    category: str
    status: str
    article_count: int = 0
    file_path: Path | None = None
    push_status: str = "not_attempted"
    error: str | None = None


@dataclass(frozen=True, slots=True)
class BriefingRunReport:
    """The aggregate outcome of one briefing run across all briefing categories."""

    date: str
    categories: tuple[BriefingCategoryReport, ...]


class BriefingService:
    """Generate one markdown briefing per category without touching the podcast flow."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        collection_service: SourceCollectionService,
        article_service: ArticleService,
        extractor: ContentExtractor,
        news_processor: NewsProcessor,
        llm_provider: LLMProvider,
        notifier: WeComNotifier | None,
        *,
        window_hours: int = 24,
        max_items_per_category: int = 10,
        max_evidence_chars_per_article: int = 800,
        output_dir: Path,
        timezone: str = "Asia/Shanghai",
        clock: Clock | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._collection_service = collection_service
        self._article_service = article_service
        self._extractor = extractor
        self._news_processor = news_processor
        self._llm_provider = llm_provider
        self._notifier = notifier
        self._window_hours = window_hours
        self._max_items_per_category = max_items_per_category
        self._max_evidence_chars_per_article = max_evidence_chars_per_article
        self._output_dir = output_dir
        self._timezone = ZoneInfo(timezone)
        self._clock = clock or Clock()

    async def run(self) -> BriefingRunReport:
        """Collect, generate, persist, and optionally push every configured category."""
        now = self._clock.now()
        briefing_date = now.astimezone(self._timezone).date()
        sources = self._briefing_sources()
        if not sources:
            logger.warning("briefing found no enabled sources tagged with briefing_category")
        window = CollectionWindow(
            start=now - timedelta(hours=self._window_hours),
            end=now,
        )
        collection = await self._collection_service.collect_sources(sources, window)
        await self._extract_missing_bodies(collection.article_ids)
        filtered = self._news_processor.filter(collection.article_ids)
        evidence_by_category = self._build_evidence(filtered.eligible_article_ids)
        reports: list[BriefingCategoryReport] = []
        for category in CATEGORY_TITLES:
            evidence = evidence_by_category.get(category, ())
            reports.append(await self._run_category(category, briefing_date, evidence))
        return BriefingRunReport(date=briefing_date.isoformat(), categories=tuple(reports))

    def _briefing_sources(self) -> tuple[Source, ...]:
        """Select only enabled sources whose config opts into a briefing category."""
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            return tuple(
                source
                for source in SourceRepository(unit.session).list()
                if source.enabled and _briefing_category(source) is not None
            )

    async def _extract_missing_bodies(self, article_ids: tuple[int, ...]) -> None:
        """Extract each candidate separately so one bad page does not stop the run."""
        for target in self._article_service.extraction_targets(article_ids):
            extracted = await self._extractor.extract(
                target.url,
                FetchPolicy(timeout_seconds=target.timeout_seconds),
            )
            if extracted.error is None:
                self._article_service.apply_extraction(target.article_id, extracted)
            else:
                self._article_service.record_extraction_failure(target.article_id, extracted)

    def _build_evidence(
        self, eligible_article_ids: tuple[int, ...]
    ) -> dict[str, tuple[BriefingEvidence, ...]]:
        """Group bounded evidence by category, best sources and newest articles first."""
        grouped: dict[str, list[tuple[tuple[int, float, int], BriefingEvidence]]] = {}
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            for article in ArticleRepository(unit.session).list_by_ids(eligible_article_ids):
                if not article.content_text:
                    continue
                category = _briefing_category(article.source)
                if category is None:
                    continue
                evidence = BriefingEvidence(
                    title=article.title,
                    source_name=article.source.name,
                    published_at=article.published_at,
                    excerpt=article.content_text[: self._max_evidence_chars_per_article],
                    source_url=article.url,
                )
                sort_key = (
                    -article.source.priority,
                    -_utc_timestamp(article.published_at or article.discovered_at),
                    -article.id,
                )
                grouped.setdefault(category, []).append((sort_key, evidence))
        return {
            category: tuple(
                evidence
                for _, evidence in sorted(entries, key=lambda entry: entry[0])[
                    : self._max_items_per_category
                ]
            )
            for category, entries in grouped.items()
        }

    async def _run_category(
        self,
        category: str,
        briefing_date: date,
        evidence: tuple[BriefingEvidence, ...],
    ) -> BriefingCategoryReport:
        """Generate one category briefing while isolating its failure from siblings."""
        if not evidence:
            logger.info(
                "briefing category skipped: no eligible articles",
                extra={"category": category},
            )
            return BriefingCategoryReport(category=category, status="skipped")
        try:
            markdown = await self._generate_markdown(category, briefing_date, evidence)
            file_path = self._write_markdown(briefing_date, category, markdown)
        except Exception as error:
            logger.exception("briefing category generation failed", extra={"category": category})
            return BriefingCategoryReport(
                category=category,
                status="failed",
                article_count=len(evidence),
                error=str(error),
            )
        push_status = await self._push(markdown)
        return BriefingCategoryReport(
            category=category,
            status="generated",
            article_count=len(evidence),
            file_path=file_path,
            push_status=push_status,
        )

    async def _generate_markdown(
        self,
        category: str,
        briefing_date: date,
        evidence: tuple[BriefingEvidence, ...],
    ) -> str:
        """Ask the LLM for prose, then render links deterministically from evidence."""
        messages = build_briefing_messages(CATEGORY_TITLES[category], evidence)
        structured = await self._llm_provider.generate_structured(
            operation=LLMOperation.GENERATE_BRIEFING,
            messages=messages,
            response_schema=BriefingResult,
            model_options={},
        )
        result = BriefingResult.model_validate(structured.content)
        return truncate_markdown(
            render_briefing(CATEGORY_TITLES[category], briefing_date, result, evidence)
        )

    def _write_markdown(self, briefing_date: date, category: str, markdown: str) -> Path:
        """Persist one briefing below the configured briefing work directory."""
        target = self._output_dir / f"{briefing_date.isoformat()}-{category}.md"
        _atomic_write(target, markdown)
        return target

    async def _push(self, markdown: str) -> str:
        """Push one rendered briefing without turning a push failure into a lost file."""
        if self._notifier is None:
            return "disabled"
        try:
            await self._notifier.push(markdown)
        except Exception:
            logger.exception("briefing wecom push failed")
            return "failed"
        return "sent"


def read_briefings_for_date(output_dir: Path, briefing_date: date) -> dict[str, str]:
    """Read persisted category briefings for one business date, if any exist."""
    briefings: dict[str, str] = {}
    for category in CATEGORY_TITLES:
        path = output_dir / f"{briefing_date.isoformat()}-{category}.md"
        if path.is_file():
            briefings[category] = path.read_text(encoding="utf-8")
    return briefings


def _briefing_category(source: Source) -> str | None:
    """Read the opt-in briefing tag without trusting arbitrary config JSON shapes."""
    try:
        config = json.loads(source.config_json)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(config, dict):
        return None
    category = config.get("briefing_category")
    if isinstance(category, str) and category in CATEGORY_TITLES:
        return category
    return None


def _utc_timestamp(value: datetime) -> float:
    """Normalize SQLite-like naive values before ordering articles by recency."""
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.timestamp()


def _atomic_write(target: Path, content: str) -> None:
    """Replace one destination only after a complete UTF-8 temporary file has been written."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, target)
