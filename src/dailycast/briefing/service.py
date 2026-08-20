"""Independent daily text briefing flow: collect, filter, generate, render, push."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session, sessionmaker

from dailycast.briefing.prompt import build_briefing_messages
from dailycast.briefing.renderer import render_briefing, truncate_markdown
from dailycast.briefing.schemas import BriefingEvidence, BriefingResult
from dailycast.briefing.webhook import WebhookNotifier
from dailycast.core.time import Clock
from dailycast.db.models import LLMOperation, Source
from dailycast.db.repositories import ArticleRepository, SourceRepository
from dailycast.db.transactions import UnitOfWork
from dailycast.llm.budget import BudgetController, BudgetReservingLLMProvider
from dailycast.llm.contracts import LLMProvider
from dailycast.llm.providers.failover import FailoverLLMProvider
from dailycast.news.service import NewsProcessor
from dailycast.sources.contracts import CollectionWindow
from dailycast.sources.extraction import ContentExtractor, FetchPolicy
from dailycast.sources.service import (
    ArticleService,
    SourceCollectionService,
    briefing_category_for_source,
)

logger = logging.getLogger(__name__)

CATEGORY_TITLES: dict[str, str] = {
    "telecom": "通信行业日报",
    "ai": "AI 动态日报",
}

ALREADY_COMPLETED = "already_completed"
NO_ELIGIBLE_ARTICLES = "no_eligible_articles"


class BriefingRunInProgressError(RuntimeError):
    """A second briefing run was requested while one still holds the run lock."""


@dataclass(frozen=True, slots=True)
class BriefingCategoryReport:
    """The per-category outcome of one briefing run."""

    category: str
    status: str
    article_count: int = 0
    file_path: Path | None = None
    push_status: str = "not_attempted"
    error: str | None = None
    reason: str | None = None


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
        notifier: WebhookNotifier | None,
        *,
        window_hours: int = 24,
        max_items_per_category: int = 10,
        max_evidence_chars_per_article: int = 800,
        output_dir: Path,
        budget_factory: Callable[[], BudgetController],
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
        self._budget_factory = budget_factory
        self._timezone = ZoneInfo(timezone)
        self._clock = clock or Clock()
        self._run_reserved = False

    @property
    def run_in_progress(self) -> bool:
        """Report whether the single briefing run slot is currently reserved."""
        return self._run_reserved

    def _try_reserve_run(self) -> None:
        """Claim the one run slot synchronously, before any coroutine can interleave.

        A boolean flag is enough here because every caller runs on the same event
        loop: between the check and the assignment there is no await, so two
        callers in one loop tick can never both reserve the slot.
        """
        if self._run_reserved:
            raise BriefingRunInProgressError("briefing run already in progress")
        self._run_reserved = True

    def create_run_task(self, *, force: bool = False) -> asyncio.Task[BriefingRunReport]:
        """Start one background run, failing fast when another run is in progress."""
        self._try_reserve_run()
        try:
            return asyncio.create_task(self._run_reserved_body(force=force))
        except Exception:
            self._run_reserved = False
            raise

    async def run(self, *, force: bool = False) -> BriefingRunReport:
        """Run at most one briefing at a time across manual and scheduled triggers."""
        self._try_reserve_run()
        return await self._run_reserved_body(force=force)

    async def _run_reserved_body(self, *, force: bool) -> BriefingRunReport:
        """Execute one reserved run and always release the slot afterwards."""
        try:
            return await self._run_once(force=force)
        finally:
            self._run_reserved = False

    async def _run_once(self, *, force: bool) -> BriefingRunReport:
        """Collect, generate, persist, and optionally push every configured category."""
        now = self._clock.now()
        briefing_date = now.astimezone(self._timezone).date()
        budget = self._budget_factory()
        provider = _budgeted_provider(self._llm_provider, budget)
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
            reports.append(
                await self._run_category(category, briefing_date, evidence, provider, force=force)
            )
        return BriefingRunReport(date=briefing_date.isoformat(), categories=tuple(reports))

    def _briefing_sources(self) -> tuple[Source, ...]:
        """Select only enabled sources whose config opts into a known briefing category."""
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            return tuple(
                source
                for source in SourceRepository(unit.session).list()
                if source.enabled and briefing_category_for_source(source) in CATEGORY_TITLES
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
                category = briefing_category_for_source(article.source)
                if category is None or category not in CATEGORY_TITLES:
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
            category: _interleave_by_source(entries)[: self._max_items_per_category]
            for category, entries in grouped.items()
        }

    async def _run_category(
        self,
        category: str,
        briefing_date: date,
        evidence: tuple[BriefingEvidence, ...],
        provider: LLMProvider,
        *,
        force: bool,
    ) -> BriefingCategoryReport:
        """Generate one category briefing while isolating its failure from siblings."""
        marker_path = self._marker_path(briefing_date, category)
        if not force and marker_path.is_file():
            logger.info(
                "briefing category already completed today",
                extra={"category": category},
            )
            return BriefingCategoryReport(
                category=category, status="skipped", reason=ALREADY_COMPLETED
            )
        if not evidence:
            logger.info(
                "briefing category skipped: no eligible articles",
                extra={"category": category},
            )
            return BriefingCategoryReport(
                category=category, status="skipped", reason=NO_ELIGIBLE_ARTICLES
            )
        try:
            markdown = await self._generate_markdown(category, briefing_date, evidence, provider)
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
        if push_status != "failed":
            # The marker is the per-day completion record; a failed push deliberately
            # leaves it absent so the next non-force run retries this category.
            _atomic_write(marker_path, f"{self._clock.now().isoformat()}\n")
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
        provider: LLMProvider,
    ) -> str:
        """Ask the LLM for prose, then render links deterministically from evidence."""
        messages = build_briefing_messages(CATEGORY_TITLES[category], evidence)
        structured = await provider.generate_structured(
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

    def _marker_path(self, briefing_date: date, category: str) -> Path:
        """Locate the per-day per-category completion marker beside the markdown file."""
        return self._output_dir / f"{briefing_date.isoformat()}-{category}.done"

    async def push_test(self) -> str:
        """Push one fixed timestamped markdown so the webhook channel can be debugged.

        Unlike a real run this touches neither sources nor the LLM; it exercises
        only the push path, and push failures propagate to the caller instead of
        being folded into a category report.
        """
        if self._notifier is None:
            return "disabled"
        triggered_at = self._clock.now().astimezone(self._timezone)
        markdown = truncate_markdown(
            "# DailyCast 简报推送测试\n\n"
            "收到这条消息，说明当前配置的简报推送通道可用。\n\n"
            f"- 触发时间：{triggered_at.isoformat(timespec='seconds')}\n"
            f"- 类目：{'、'.join(CATEGORY_TITLES.values())}\n"
        )
        await self._notifier.push(markdown)
        return "sent"

    async def _push(self, markdown: str) -> str:
        """Push one rendered briefing without turning a push failure into a lost file."""
        if self._notifier is None:
            return "disabled"
        try:
            await self._notifier.push(markdown)
        except Exception:
            logger.exception("briefing webhook push failed")
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


_BRIEFING_FILE_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})-[a-z0-9_]+\.md$")


def latest_briefing_date(output_dir: Path) -> date | None:
    """Return the most recent business date that has at least one persisted briefing.

    Reading "latest" must survive the window after midnight in which today's run has
    not happened yet: the previous day's briefings are still the latest available.
    """
    dates: set[date] = set()
    if not output_dir.is_dir():
        return None
    for path in output_dir.glob("*.md"):
        match = _BRIEFING_FILE_DATE.match(path.name)
        if match is None:
            continue
        try:
            dates.add(date.fromisoformat(match.group(1)))
        except ValueError:
            continue
    return max(dates) if dates else None


def _budgeted_provider(provider: LLMProvider, budget: BudgetController) -> LLMProvider:
    """Reserve budget per real provider attempt for one briefing run.

    A failover chain turns one logical call into up to two physical attempts,
    so each leg is wrapped separately and reassembled: the primary and the
    fallback each reserve with their own output-token allowance right before
    they are actually invoked.
    """
    if isinstance(provider, FailoverLLMProvider):
        return FailoverLLMProvider(
            BudgetReservingLLMProvider(provider.primary, budget),
            BudgetReservingLLMProvider(provider.fallback, budget),
        )
    return BudgetReservingLLMProvider(provider, budget)


def _utc_timestamp(value: datetime) -> float:
    """Normalize SQLite-like naive values before ordering articles by recency."""
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.timestamp()


def _interleave_by_source(
    entries: list[tuple[tuple[int, float, int], BriefingEvidence]],
) -> tuple[BriefingEvidence, ...]:
    """Rotate evidence across sources so one feed cannot fill a whole category.

    Entries arrive with a (priority, recency, id) sort key. Sources are ordered
    by their best entry and then take turns; within a source the ranked order
    is preserved. A category fed by a single source degrades to plain ranking.
    """
    queues: dict[str, list[BriefingEvidence]] = {}
    for _, evidence in sorted(entries, key=lambda entry: entry[0]):
        queues.setdefault(evidence.source_name, []).append(evidence)
    rotation = list(queues.values())
    picked: list[BriefingEvidence] = []
    while rotation:
        for queue in rotation[:]:
            picked.append(queue.pop(0))
            if not queue:
                rotation.remove(queue)
    return tuple(picked)


def _atomic_write(target: Path, content: str) -> None:
    """Replace one destination only after a complete UTF-8 temporary file has been written."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, target)
