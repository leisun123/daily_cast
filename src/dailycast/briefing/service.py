"""Independent daily text briefing flow: collect, filter, generate, render, push."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from math import ceil
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session, sessionmaker

from dailycast.briefing.alerts import BriefingAlert
from dailycast.briefing.prompt import (
    build_briefing_messages,
    build_briefing_repair_messages,
    build_merged_focus_messages,
)
from dailycast.briefing.renderer import (
    render_briefing,
    render_merged_briefing,
    truncate_markdown,
)
from dailycast.briefing.schemas import (
    MAX_BRIEFING_ITEMS,
    BriefingEvidence,
    BriefingItem,
    BriefingResult,
    MergedBriefingFocus,
)
from dailycast.briefing.selection import (
    BriefingSelectionCandidate,
    BriefingSelectionPolicy,
    RankedBriefingEvidence,
    publisher_key,
    select_evidence,
)
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
ALREADY_PREPARED = "already_prepared"
NO_ELIGIBLE_ARTICLES = "no_eligible_articles"
NOT_PREPARED = "not_prepared"


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


@dataclass(frozen=True, slots=True)
class _PendingBriefingDelivery:
    """A persisted category waiting for the one combined WeCom push."""

    category: str
    result: BriefingResult
    evidence: tuple[RankedBriefingEvidence, ...]


@dataclass(frozen=True, slots=True)
class _PreparedBriefing:
    """The atomically persisted combined message waiting for its delivery tick."""

    run_date: date
    briefing_date: date
    categories: tuple[str, ...]
    markdown: str


@dataclass(frozen=True, slots=True)
class _PreparedRun:
    """Preparation output plus whether this invocation should immediately deliver it."""

    run_date: date
    briefing_date: date
    report: BriefingRunReport
    should_deliver: bool


class BriefingService:
    """Generate independently selected category briefs and one merged WeCom delivery."""

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
        alert: BriefingAlert | None = None,
        window_hours: int = 24,
        max_items_per_category: int = 10,
        max_evidence_chars_per_article: int = 800,
        output_dir: Path,
        budget_factory: Callable[[], BudgetController],
        briefing_source_ids: frozenset[str] | None = None,
        selection_policy: BriefingSelectionPolicy,
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
        self._alert = alert
        self._window_hours = window_hours
        self._max_items_per_category = max_items_per_category
        self._max_evidence_chars_per_article = max_evidence_chars_per_article
        self._output_dir = output_dir
        self._budget_factory = budget_factory
        self._briefing_source_ids = briefing_source_ids
        self._selection_policy = selection_policy
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
        """Manually prepare and immediately deliver one briefing in the reserved slot."""
        self._try_reserve_run()
        return await self._run_reserved_body(force=force)

    async def prepare(self, *, force: bool = False) -> BriefingRunReport:
        """Collect and persist the report before 08:30, without touching the webhook."""
        self._try_reserve_run()
        try:
            return (await self._prepare_once(force=force)).report
        finally:
            self._run_reserved = False

    async def deliver_prepared(self) -> BriefingRunReport:
        """Post only the report prepared for this delivery day; never generate at 08:30."""
        self._try_reserve_run()
        try:
            now = self._clock.now()
            window = self._collection_window(now)
            run_date = now.astimezone(self._timezone).date()
            briefing_date = (
                window.end.astimezone(self._timezone) - timedelta(microseconds=1)
            ).date()
            return await self._deliver_prepared_once(run_date, briefing_date)
        finally:
            self._run_reserved = False

    async def _run_reserved_body(self, *, force: bool) -> BriefingRunReport:
        """Execute the manual prepare-and-deliver path and always release the slot."""
        try:
            prepared = await self._prepare_once(force=force)
            if not prepared.should_deliver:
                return prepared.report
            return await self._deliver_prepared_once(
                prepared.run_date,
                prepared.briefing_date,
                prepared_report=prepared.report,
                force=force,
            )
        finally:
            self._run_reserved = False

    async def _prepare_once(self, *, force: bool) -> _PreparedRun:
        """Collect, generate, and atomically persist the combined message for later delivery."""
        now = self._clock.now()
        window = self._collection_window(now)
        run_date = now.astimezone(self._timezone).date()
        briefing_date = (window.end.astimezone(self._timezone) - timedelta(microseconds=1)).date()
        existing = None if force else self._load_prepared_briefing(run_date, briefing_date)
        if existing is not None:
            all_delivered = all(
                self._marker_path(run_date, category).is_file() for category in existing.categories
            )
            reason = ALREADY_COMPLETED if all_delivered else ALREADY_PREPARED
            existing_report = BriefingRunReport(
                date=briefing_date.isoformat(),
                categories=tuple(
                    BriefingCategoryReport(
                        category=category,
                        status="skipped",
                        file_path=self._output_dir / f"{briefing_date.isoformat()}-{category}.md",
                        reason=reason,
                    )
                    for category in existing.categories
                ),
            )
            return _PreparedRun(
                run_date=run_date,
                briefing_date=briefing_date,
                report=existing_report,
                should_deliver=not all_delivered,
            )
        budget = self._budget_factory()
        provider = _budgeted_provider(self._llm_provider, budget)
        sources = self._briefing_sources()
        if not sources:
            logger.warning("briefing found no enabled sources tagged with briefing_category")
        collection = await self._collection_service.collect_sources(sources, window)
        await self._extract_missing_bodies(collection.article_ids)
        verified_article_ids = await self._verify_reader_links(collection.article_ids)
        minimum_freshness_hours = ceil((now - window.start).total_seconds() / 3600)
        filtered = self._news_processor.filter(
            verified_article_ids,
            minimum_max_age_hours=minimum_freshness_hours,
        )
        deduplicated = self._news_processor.deduplicate_in_memory(filtered.eligible_article_ids)
        evidence_by_category = self._build_evidence(deduplicated.primary_article_ids, window)
        reports: list[BriefingCategoryReport] = []
        pending_deliveries: list[_PendingBriefingDelivery] = []
        for category in CATEGORY_TITLES:
            evidence = evidence_by_category.get(category, ())
            category_report, pending_delivery = await self._run_category(
                category,
                briefing_date,
                evidence,
                provider,
                marker_date=run_date,
                force=force,
            )
            reports.append(category_report)
            if pending_delivery is not None:
                pending_deliveries.append(pending_delivery)
        if pending_deliveries:
            merged_focus = (
                None
                if any(delivery.result.degraded for delivery in pending_deliveries)
                else await self._generate_merged_focus(pending_deliveries, provider)
            )
            merged_markdown = self._render_merged_markdown(
                briefing_date, pending_deliveries, focus=merged_focus
            )
            self._write_merged_markdown(briefing_date, merged_markdown)
            self._write_prepared_marker(run_date, briefing_date, pending_deliveries)
        prepared_report = BriefingRunReport(
            date=briefing_date.isoformat(), categories=tuple(reports)
        )
        return _PreparedRun(
            run_date=run_date,
            briefing_date=briefing_date,
            report=prepared_report,
            should_deliver=bool(pending_deliveries),
        )

    async def _deliver_prepared_once(
        self,
        run_date: date,
        briefing_date: date,
        *,
        prepared_report: BriefingRunReport | None = None,
        force: bool = False,
    ) -> BriefingRunReport:
        """Post a complete saved message and only then mark its categories delivered."""
        prepared = self._load_prepared_briefing(run_date, briefing_date)
        if prepared is None:
            logger.error(
                "briefing delivery skipped: no complete prepared message",
                extra={
                    "run_date": run_date.isoformat(),
                    "briefing_date": briefing_date.isoformat(),
                },
            )
            await self._alert_message("日报未准备好", RuntimeError(NOT_PREPARED))
            return BriefingRunReport(
                date=briefing_date.isoformat(),
                categories=tuple(
                    BriefingCategoryReport(
                        category=category, status="failed", reason=NOT_PREPARED
                    )
                    for category in CATEGORY_TITLES
                ),
            )
        if not force and all(
            self._marker_path(run_date, category).is_file() for category in prepared.categories
        ):
            return BriefingRunReport(
                date=briefing_date.isoformat(),
                categories=tuple(
                    BriefingCategoryReport(
                        category=category, status="skipped", reason=ALREADY_COMPLETED
                    )
                    for category in prepared.categories
                ),
            )

        push_status = await self._push(prepared.markdown)
        if push_status != "failed":
            for category in prepared.categories:
                _atomic_write(
                    self._marker_path(run_date, category),
                    f"{self._clock.now().isoformat()}\n",
                )

        if prepared_report is not None:
            reports = tuple(
                replace(report, push_status=push_status)
                if report.category in prepared.categories
                else report
                for report in prepared_report.categories
            )
        else:
            reports = tuple(
                BriefingCategoryReport(
                    category=category,
                    status="generated",
                    file_path=self._output_dir / f"{briefing_date.isoformat()}-{category}.md",
                    push_status=push_status,
                )
                for category in prepared.categories
            )
        return BriefingRunReport(date=briefing_date.isoformat(), categories=reports)

    def _collection_window(self, now: datetime) -> CollectionWindow:
        """Use the previous Shanghai calendar day, or Friday-Sunday on Monday."""
        local_now = now.astimezone(self._timezone)
        end_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        days_back = 3 if local_now.weekday() == 0 else 1
        start_local = end_local - timedelta(days=days_back)
        return CollectionWindow(start=start_local.astimezone(UTC), end=end_local.astimezone(UTC))

    def _briefing_sources(self) -> tuple[Source, ...]:
        """Select enabled, current-policy sources whose config opts into a briefing category."""
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            return tuple(
                source
                for source in SourceRepository(unit.session).list()
                if source.enabled
                and briefing_category_for_source(source) in CATEGORY_TITLES
                and (self._briefing_source_ids is None or source.id in self._briefing_source_ids)
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

    async def _verify_reader_links(self, article_ids: tuple[int, ...]) -> tuple[int, ...]:
        """Keep only source pages that can be opened from the delivery environment."""
        verified: list[int] = []
        for target in self._article_service.verification_targets(article_ids):
            extracted = await self._extractor.extract(
                target.url,
                FetchPolicy(timeout_seconds=target.timeout_seconds),
            )
            if extracted.error is None:
                verified.append(target.article_id)
                continue
            logger.warning(
                "briefing dropped article with unreachable reader link",
                extra={
                    "article_id": target.article_id,
                    "source_error_code": extracted.error.code,
                },
            )
        return tuple(verified)

    def _build_evidence(
        self, eligible_article_ids: tuple[int, ...], window: CollectionWindow
    ) -> dict[str, tuple[RankedBriefingEvidence, ...]]:
        """Build in-window, publisher-balanced candidates for LLM editorial selection."""
        grouped: dict[str, list[BriefingSelectionCandidate]] = {}
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            for article in ArticleRepository(unit.session).list_by_ids(eligible_article_ids):
                if not article.content_text:
                    continue
                if article.published_at_inferred or not window.includes(article.published_at):
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
                candidate = BriefingSelectionCandidate(
                    article_id=article.id,
                    source_id=article.source_id,
                    source_priority=article.source.priority,
                    discovered_at=article.discovered_at,
                    evidence=evidence,
                )
                grouped.setdefault(category, []).append(candidate)
        return {
            category: select_evidence(
                category,
                candidates,
                self._selection_policy,
                limit=(
                    self._selection_policy.category(category).editorial_candidate_limit
                    if self._selection_policy.category(category).editorial_selection
                    else self._max_items_per_category
                ),
            )
            for category, candidates in grouped.items()
        }

    async def _run_category(
        self,
        category: str,
        briefing_date: date,
        evidence: tuple[RankedBriefingEvidence, ...],
        provider: LLMProvider,
        *,
        marker_date: date,
        force: bool,
    ) -> tuple[BriefingCategoryReport, _PendingBriefingDelivery | None]:
        """Generate one category while deferring delivery until every category is ready."""
        marker_path = self._marker_path(marker_date, category)
        if not force and marker_path.is_file():
            logger.info(
                "briefing category already completed today",
                extra={"category": category},
            )
            return (
                BriefingCategoryReport(
                    category=category, status="skipped", reason=ALREADY_COMPLETED
                ),
                None,
            )
        if not evidence:
            logger.info(
                "briefing category skipped: no eligible articles",
                extra={"category": category},
            )
            return (
                BriefingCategoryReport(
                    category=category, status="skipped", reason=NO_ELIGIBLE_ARTICLES
                ),
                None,
            )
        try:
            result = await self._generate_result(category, evidence, provider)
        except Exception as error:
            await self._alert_message(f"消息生成（{CATEGORY_TITLES[category]}）", error)
            logger.exception(
                "briefing model generation failed; using evidence fallback",
                extra={"category": category, "error": str(error)},
            )
            result = self._fallback_result(category, evidence)
        markdown = self._render_markdown(category, briefing_date, result, evidence)
        try:
            file_path = self._write_markdown(briefing_date, category, markdown)
        except Exception as error:
            logger.exception("briefing category persistence failed", extra={"category": category})
            return (
                BriefingCategoryReport(
                    category=category,
                    status="failed",
                    article_count=len(evidence),
                    error=str(error),
                ),
                None,
            )
        return (
            BriefingCategoryReport(
                category=category,
                status="generated",
                article_count=len(evidence),
                file_path=file_path,
            ),
            _PendingBriefingDelivery(category=category, result=result, evidence=evidence),
        )

    async def _generate_result(
        self,
        category: str,
        evidence: tuple[RankedBriefingEvidence, ...],
        provider: LLMProvider,
    ) -> BriefingResult:
        """Ask the LLM for prose, then retain only evidence-backed entries."""
        messages = build_briefing_messages(
            CATEGORY_TITLES[category],
            evidence,
            category=category,
            editorial_selection=self._selection_policy.category(category).editorial_selection,
        )
        structured = await provider.generate_structured(
            operation=LLMOperation.GENERATE_BRIEFING,
            messages=messages,
            response_schema=BriefingResult,
            model_options={},
        )
        result = BriefingResult.model_validate(structured.content)
        policy = self._selection_policy.category(category)
        audited = _audit_generated_result(
            result,
            evidence,
            allow_evidence_backfill=not policy.editorial_selection,
        )
        # The model, not a deterministic publisher quota or title-length rule,
        # decides whether a verified candidate belongs in the management brief.
        # Local auditing is limited to objective link identity and duplicate URL
        # checks; the prompt asks the editor to consider source diversity.
        target_count = _target_item_count(evidence)
        if not policy.editorial_selection or len(audited.items) >= target_count:
            return audited

        accepted_urls = {item.source_url for item in audited.items}
        remaining = tuple(
            entry for entry in evidence if entry.evidence.source_url not in accepted_urls
        )
        missing_count = target_count - len(audited.items)
        repair_structured = await provider.generate_structured(
            operation=LLMOperation.GENERATE_BRIEFING,
            messages=build_briefing_repair_messages(
                CATEGORY_TITLES[category],
                category,
                remaining,
                audited.items,
                missing_count=missing_count,
            ),
            response_schema=BriefingResult,
            model_options={},
        )
        repair = BriefingResult.model_validate(repair_structured.content)
        combined = BriefingResult(
            overview=audited.overview,
            items=[*audited.items, *repair.items],
        )
        repaired = _audit_generated_result(
            combined,
            evidence,
            allow_evidence_backfill=False,
        )
        if len(repaired.items) < target_count:
            logger.warning(
                "briefing editorial selection remained below target after repair; "
                "retaining the verified LLM-written items",
                extra={
                    "category": category,
                    "selected_count": len(repaired.items),
                    "target_count": target_count,
                },
            )
        return repaired

    async def _generate_merged_focus(
        self,
        pending_deliveries: list[_PendingBriefingDelivery],
        provider: LLMProvider,
    ) -> str | None:
        """Write the one lead sentence after selection, without changing selected items."""
        try:
            structured = await provider.generate_structured(
                operation=LLMOperation.GENERATE_BRIEFING,
                messages=build_merged_focus_messages(
                    [
                        (CATEGORY_TITLES[delivery.category], delivery.result)
                        for delivery in pending_deliveries
                    ]
                ),
                response_schema=MergedBriefingFocus,
                model_options={},
            )
            return MergedBriefingFocus.model_validate(structured.content).focus
        except Exception:
            logger.exception("briefing merged focus generation failed; sending title list only")
            return None

    def _fallback_result(
        self,
        category: str,
        evidence: tuple[RankedBriefingEvidence, ...],
    ) -> BriefingResult:
        """Build a clearly labelled six-title result when no model remains usable."""
        items: list[BriefingItem] = []
        for entry in evidence:
            if len(items) == MAX_BRIEFING_ITEMS:
                break
            title = entry.evidence.title.strip().rstrip("。！？!?")
            try:
                items.append(
                    BriefingItem(
                        headline=title,
                        summary="模型服务暂时不可用，未生成新闻摘要。",
                        why_it_matters="仅保留已核验的原文标题与链接。",
                        source_name=entry.evidence.source_name,
                        source_url=entry.evidence.source_url,
                    )
                )
            except ValueError:
                _log_incomplete_fallback_title(entry)
        return BriefingResult(
            overview=(
                f"模型服务暂时不可用，以下为 {CATEGORY_TITLES[category]}"
                "已核验原文标题降级列表。"
            ),
            items=items,
            degraded=True,
        )

    def _render_markdown(
        self,
        category: str,
        briefing_date: date,
        result: BriefingResult,
        evidence: tuple[RankedBriefingEvidence, ...],
    ) -> str:
        """Render and enforce the one actual WeCom byte limit for any valid briefing."""
        return truncate_markdown(
            render_briefing(
                CATEGORY_TITLES[category],
                briefing_date,
                result,
                [entry.evidence for entry in evidence],
            )
        )

    def _render_merged_markdown(
        self,
        briefing_date: date,
        pending_deliveries: list[_PendingBriefingDelivery],
        *,
        focus: str | None,
    ) -> str:
        """Use the compact preview layout for the one group-message delivery."""
        section_labels = {"telecom": ("通信", "📡 通信"), "ai": ("AI", "🤖 AI")}
        return render_merged_briefing(
            briefing_date,
            [
                (
                    *section_labels[delivery.category],
                    delivery.result,
                    [entry.evidence for entry in delivery.evidence],
                )
                for delivery in pending_deliveries
            ],
            focus=focus,
        )

    def _write_markdown(self, briefing_date: date, category: str, markdown: str) -> Path:
        """Persist one briefing below the configured briefing work directory."""
        target = self._output_dir / f"{briefing_date.isoformat()}-{category}.md"
        _atomic_write(target, markdown)
        return target

    def _write_merged_markdown(self, briefing_date: date, markdown: str) -> Path:
        """Persist the exact compact message used for delivery and acceptance checks."""
        target = self._output_dir / f"{briefing_date.isoformat()}-merged.md"
        _atomic_write(target, markdown)
        return target

    def _marker_path(self, briefing_date: date, category: str) -> Path:
        """Locate the per-day per-category completion marker beside the markdown file."""
        return self._output_dir / f"{briefing_date.isoformat()}-{category}.done"

    def _prepared_marker_path(self, run_date: date) -> Path:
        """Locate the one atomic marker that makes a combined message safe to send."""
        return self._output_dir / f"{run_date.isoformat()}-merged.prepared.json"

    def _write_prepared_marker(
        self,
        run_date: date,
        briefing_date: date,
        deliveries: list[_PendingBriefingDelivery],
    ) -> None:
        """Record only a fully written combined message as ready for the 08:30 tick."""
        payload = {
            "briefing_date": briefing_date.isoformat(),
            "categories": [delivery.category for delivery in deliveries],
        }
        _atomic_write(
            self._prepared_marker_path(run_date),
            f"{json.dumps(payload, ensure_ascii=False, sort_keys=True)}\n",
        )

    def _load_prepared_briefing(
        self, run_date: date, briefing_date: date
    ) -> _PreparedBriefing | None:
        """Return one validated prepared message, never a partial or stale file."""
        marker_path = self._prepared_marker_path(run_date)
        try:
            payload = json.loads(marker_path.read_text(encoding="utf-8"))
            recorded_date = date.fromisoformat(str(payload["briefing_date"]))
            categories = tuple(str(category) for category in payload["categories"])
        except (FileNotFoundError, TypeError, ValueError, json.JSONDecodeError, KeyError):
            return None
        if recorded_date != briefing_date or not categories or any(
            category not in CATEGORY_TITLES for category in categories
        ):
            return None
        merged_path = self._output_dir / f"{briefing_date.isoformat()}-merged.md"
        try:
            markdown = merged_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        if not markdown.strip():
            return None
        return _PreparedBriefing(
            run_date=run_date,
            briefing_date=briefing_date,
            categories=categories,
            markdown=markdown,
        )

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

    async def _alert_message(self, stage: str, error: Exception) -> None:
        """Notify the monitoring robot without changing the briefing's fallback behavior."""
        if self._alert is None:
            return
        await self._alert(stage, error)

    async def _push(self, markdown: str) -> str:
        """Push one rendered briefing without turning a push failure into a lost file."""
        if self._notifier is None:
            return "disabled"
        try:
            await self._notifier.push(markdown)
        except Exception as error:
            await self._alert_message("企业微信发送", error)
            logger.exception("briefing webhook push failed")
            return "failed"
        return "sent"


def _audit_generated_result(
    result: BriefingResult,
    evidence: tuple[RankedBriefingEvidence, ...],
    *,
    publisher_cap: int | None = None,
    fallback_publisher_cap: int | None = None,
    allow_evidence_backfill: bool = True,
) -> BriefingResult:
    """Retain only exact evidence links and optionally fill omitted fixed-order evidence.

    Link reachability and publication date are checked before generation.  This
    final local gate handles a different failure mode: a valid model response
    can still point an item at a publisher-level fallback URL. Every delivered
    item must therefore map to one exact evidence URL. Fixed-order report modes
    may fill an omitted slot from verified evidence; editorial modes never do,
    because only the LLM may decide that a candidate is management-relevant.
    """
    target_count = _target_item_count(
        evidence,
        publisher_cap=publisher_cap,
        fallback_publisher_cap=fallback_publisher_cap,
    )
    if target_count == 0:
        return BriefingResult(overview=result.overview, items=[])

    effective_publisher_cap = _effective_publisher_cap(
        evidence,
        publisher_cap=publisher_cap,
        fallback_publisher_cap=fallback_publisher_cap,
    )

    evidence_by_url = {entry.evidence.source_url: entry for entry in evidence}
    accepted: list[BriefingItem] = []
    seen_urls: set[str] = set()
    publisher_counts: dict[str, int] = {}
    dropped_count = 0
    for item in result.items:
        entry = evidence_by_url.get(item.source_url)
        if entry is None or item.source_url in seen_urls:
            dropped_count += 1
            continue
        key = publisher_key(entry.evidence.source_url)
        if (
            effective_publisher_cap is not None
            and publisher_counts.get(key, 0) >= effective_publisher_cap
        ):
            dropped_count += 1
            continue
        accepted.append(
            item.model_copy(
                update={
                    "source_name": entry.evidence.source_name,
                    "source_url": entry.evidence.source_url,
                }
            )
        )
        seen_urls.add(item.source_url)
        publisher_counts[key] = publisher_counts.get(key, 0) + 1
        if len(accepted) == target_count:
            break

    fallback_count = 0
    if allow_evidence_backfill:
        for entry in evidence:
            if len(accepted) == target_count:
                break
            if entry.evidence.source_url in seen_urls:
                continue
            key = publisher_key(entry.evidence.source_url)
            if (
                effective_publisher_cap is not None
                and publisher_counts.get(key, 0) >= effective_publisher_cap
            ):
                continue
            fallback_item = _fallback_item_from_evidence(entry)
            if fallback_item is None:
                continue
            accepted.append(fallback_item)
            seen_urls.add(entry.evidence.source_url)
            publisher_counts[key] = publisher_counts.get(key, 0) + 1
            fallback_count += 1

    if dropped_count or fallback_count or len(accepted) < target_count:
        logger.warning(
            "briefing output audit adjusted generated items",
            extra={
                "model_item_count": len(result.items),
                "accepted_item_count": len(accepted),
                "dropped_item_count": dropped_count,
                "fallback_item_count": fallback_count,
                "allow_evidence_backfill": allow_evidence_backfill,
            },
        )
    return BriefingResult(overview=result.overview, items=accepted)


def _target_item_count(
    evidence: tuple[RankedBriefingEvidence, ...],
    *,
    publisher_cap: int | None = None,
    fallback_publisher_cap: int | None = None,
) -> int:
    """Return the audited item target shared by initial and repair selection."""
    target = min(MAX_BRIEFING_ITEMS, len(evidence))
    effective_cap = _effective_publisher_cap(
        evidence,
        publisher_cap=publisher_cap,
        fallback_publisher_cap=fallback_publisher_cap,
    )
    if effective_cap is None:
        return target
    return min(target, _publisher_capacity(evidence, effective_cap))


def _effective_publisher_cap(
    evidence: tuple[RankedBriefingEvidence, ...],
    *,
    publisher_cap: int | None,
    fallback_publisher_cap: int | None,
) -> int | None:
    """Relax the publisher ceiling only when the primary ceiling cannot fill the target."""
    if publisher_cap is None:
        return None
    target = min(MAX_BRIEFING_ITEMS, len(evidence))
    if (
        _publisher_capacity(evidence, publisher_cap) < target
        and fallback_publisher_cap is not None
    ):
        return fallback_publisher_cap
    return publisher_cap


def _publisher_capacity(
    evidence: tuple[RankedBriefingEvidence, ...], publisher_cap: int
) -> int:
    """Return how many slots a verified pool can fill under one outlet ceiling."""
    counts: dict[str, int] = {}
    for entry in evidence:
        key = publisher_key(entry.evidence.source_url)
        counts[key] = counts.get(key, 0) + 1
    return sum(min(count, publisher_cap) for count in counts.values())


def _fallback_item_from_evidence(entry: RankedBriefingEvidence) -> BriefingItem | None:
    """Render an omitted candidate only when its source title is already complete."""
    title = entry.evidence.title.strip().rstrip("。！？!?")
    if len(title) > 60:
        _log_incomplete_fallback_title(entry)
        return None
    try:
        return BriefingItem(
            headline=title,
            summary=f"原文报道：{title}。",
            why_it_matters=f"管理关注：{entry.reason}。",
            source_name=entry.evidence.source_name,
            source_url=entry.evidence.source_url,
        )
    except ValueError:
        _log_incomplete_fallback_title(entry)
        return None


def _log_incomplete_fallback_title(entry: RankedBriefingEvidence) -> None:
    """Record a discarded raw title without publishing its incomplete text."""
    logger.warning(
        "briefing omitted an incomplete evidence fallback title",
        extra={
            "source_url": entry.evidence.source_url,
            "source_name": entry.evidence.source_name,
        },
    )


def read_briefings_for_date(output_dir: Path, briefing_date: date) -> dict[str, str]:
    """Read persisted category briefings for one business date, if any exist."""
    briefings: dict[str, str] = {}
    for category in CATEGORY_TITLES:
        path = output_dir / f"{briefing_date.isoformat()}-{category}.md"
        if path.is_file():
            briefings[category] = path.read_text(encoding="utf-8")
    merged_path = output_dir / f"{briefing_date.isoformat()}-merged.md"
    if merged_path.is_file():
        briefings["merged"] = merged_path.read_text(encoding="utf-8")
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
