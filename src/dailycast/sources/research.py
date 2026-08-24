"""Native web-search discovery that persists only locally verified article pages."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from datetime import UTC
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from dailycast.core.config import WebResearchSettings
from dailycast.core.errors import (
    LLMProviderError,
    LLMWebSearchUnsupportedError,
)
from dailycast.db.models import Source
from dailycast.llm.contracts import (
    JSONValue,
    LLMMessage,
    StructuredResult,
    WebResearchProvider,
)
from dailycast.sources.contracts import (
    ArticleCandidate,
    CollectionResult,
    CollectionWindow,
    SourceError,
)
from dailycast.sources.extraction import ContentExtractor, FetchPolicy

_DISCOVERY_HOSTS = frozenset(
    {
        "google.com",
        "www.google.com",
        "bing.com",
        "www.bing.com",
        "news.google.com",
        "search.yahoo.com",
        "www.baidu.com",
    }
)
_READER_BLOCKED_HOSTS = frozenset({"qbitai.com", "www.qbitai.com"})
_RESEARCH_FACETS: dict[Literal["telecom", "ai"], tuple[str, ...]] = {
    "telecom": (
        "中国移动及国内外运营商、竞争对手的经营与网络建设动态",
        "基站、无线网/RAN、频谱许可、5G-A/6G 的建设与商用",
        "华为、中兴和网络设备、光通信等关键供应商的交付与产品动态",
        "通信监管政策、地方通信项目与专项行动",
    ),
    "ai": (
        "国内外重点大模型的发布、开源、API 和推理能力变化",
        "本地化、私有化、端侧和边缘 AI 部署",
        "中国市场适配、国产算力与合规落地",
        "企业 AI 应用、生态合作和有公开数据支撑的产品热点",
    ),
}
class WebResearchCandidate(BaseModel):
    """One untrusted discovery record returned by the model after native web search."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=300)
    url: str = Field(min_length=1, max_length=2_000)
    publisher: str = Field(min_length=1, max_length=160)
    finding: str = Field(min_length=1, max_length=500)
    published_at_hint: str | None = Field(default=None, max_length=100)


class WebResearchCandidateSet(BaseModel):
    """The model's bounded candidate response; every item still needs local verification."""

    model_config = ConfigDict(extra="forbid")

    candidates: list[WebResearchCandidate] = Field(max_length=20)


class ResearchSourceOptions(BaseModel):
    """The fixed per-category policy stored in briefing source configuration."""

    model_config = ConfigDict(extra="forbid")

    briefing_category: Literal["telecom", "ai"]
    topic: Literal["telecom", "ai"]
    query: str = Field(min_length=1, max_length=1_000)
    publisher_preference: str = Field(min_length=1, max_length=160)
    require_verified_publication_date: bool

    @model_validator(mode="after")
    def require_matching_topic_and_category(self) -> ResearchSourceOptions:
        """Keep the source's persisted briefing category deterministic."""
        if self.topic != self.briefing_category:
            raise ValueError("topic must match briefing_category")
        return self


class ResearchCollector:
    """Discover candidate links with a model, then establish local page evidence before storage."""

    def __init__(
        self,
        provider: WebResearchProvider,
        extractor: ContentExtractor,
        settings: WebResearchSettings,
    ) -> None:
        self._provider = provider
        self._extractor = extractor
        self._settings = settings

    async def collect(self, source: Source, window: CollectionWindow) -> CollectionResult:
        """Return only candidate articles whose final pages passed all local evidence checks."""
        if not self._settings.enabled:
            return CollectionResult(source_id=source.id)
        options, option_error = _parse_options(source.config_json)
        if option_error is not None:
            return CollectionResult(source_id=source.id, error=option_error)
        assert options is not None
        candidates: list[ArticleCandidate] = []
        errors: list[SourceError] = []
        discovered_records: list[tuple[str, WebResearchCandidate, StructuredResult]] = []
        successful_search_calls = 0
        seen_discovered_urls: set[str] = set()
        facets = _research_call_facets(options.topic, self._settings.max_search_calls_per_source)
        search_results = await asyncio.gather(
            *(
                self._provider.generate_web_research(
                    _research_messages(options, window, focus=facet),
                    WebResearchCandidateSet,
                    {"search_context_size": self._settings.search_context_size},
                )
                for facet in facets
            ),
            return_exceptions=True,
        )
        for facet, search_result in zip(facets, search_results, strict=True):
            if isinstance(search_result, LLMWebSearchUnsupportedError):
                return CollectionResult(
                    source_id=source.id,
                    error=SourceError(
                        code="WEB_RESEARCH_UNSUPPORTED",
                        summary="configured LLM provider does not support native web search",
                        retryable=False,
                    ),
                )
            if isinstance(search_result, LLMProviderError):
                errors.append(
                    SourceError(
                        code="WEB_RESEARCH_REQUEST_FAILED",
                        summary="native web-search request failed",
                        retryable=search_result.retryable,
                    )
                )
                continue
            if isinstance(search_result, Exception):
                errors.append(
                    SourceError(
                        code="WEB_RESEARCH_REQUEST_FAILED",
                        summary="native web-search request failed",
                        retryable=False,
                    )
                )
                continue
            structured = search_result
            try:
                discovered = WebResearchCandidateSet.model_validate(structured.content)
            except ValidationError:
                errors.append(
                    SourceError(
                        code="WEB_RESEARCH_RESPONSE_INVALID",
                        summary="native web-search returned an invalid candidate set",
                        retryable=False,
                    )
                )
                continue
            successful_search_calls += 1
            for discovered_candidate in discovered.candidates:
                candidate_key = discovered_candidate.url.casefold()
                if candidate_key in seen_discovered_urls:
                    continue
                seen_discovered_urls.add(candidate_key)
                discovered_records.append((facet, discovered_candidate, structured))

        if successful_search_calls == 0 and errors:
            return CollectionResult(source_id=source.id, error=errors[0])

        candidate_limit = min(
            source.max_items_per_run or 50,
            self._settings.max_candidates_per_source,
        )
        for facet, discovered_candidate, structured in discovered_records[:candidate_limit]:
            url_error = _candidate_url_error(discovered_candidate.url)
            if url_error is not None:
                errors.append(url_error)
                continue
            extracted = await self._extractor.extract(
                discovered_candidate.url,
                FetchPolicy(timeout_seconds=float(source.request_timeout_seconds or 20)),
            )
            if extracted.error is not None:
                errors.append(extracted.error)
                continue
            if (
                extracted.final_url is None
                or extracted.content_text is None
                or extracted.published_at is None
            ):
                errors.append(
                    SourceError(
                        code="MISSING_PUBLICATION_DATE",
                        summary="verified article did not expose a publication date",
                        retryable=False,
                    )
                )
                continue
            if not window.includes(extracted.published_at):
                errors.append(
                    SourceError(
                        code="ARTICLE_OUTSIDE_WINDOW",
                        summary=(
                            "verified article publication date is outside the collection window"
                        ),
                        retryable=False,
                    )
                )
                continue
            candidates.append(
                ArticleCandidate(
                    source_id=source.id,
                    external_id=extracted.final_url,
                    url=extracted.final_url,
                    title=discovered_candidate.title,
                    summary=discovered_candidate.finding,
                    content_text=extracted.content_text[: self._settings.max_article_chars],
                    published_at=extracted.published_at,
                    language=source.language,
                    fetched_at=extracted.fetched_at,
                    http_status=extracted.http_status,
                    metadata={
                        "candidate_url": discovered_candidate.url,
                        "discovery_method": "openai_web_search",
                        "final_url": extracted.final_url,
                        "model": structured.model,
                        "publisher": discovered_candidate.publisher,
                        "request_id": structured.request_id or "",
                        "search_facet": facet,
                        "verified_at": (extracted.fetched_at or window.end)
                        .astimezone(UTC)
                        .isoformat(),
                    },
                )
            )
        return CollectionResult(
            source_id=source.id,
            candidates=tuple(candidates),
            errors=tuple(errors),
        )


class UnavailableWebResearchProvider:
    """Make unsupported primary provider capability visible as one source-local error."""

    provider_name = "unavailable_web_research"
    model = "unavailable"

    async def generate_web_research(
        self,
        messages: Sequence[LLMMessage],
        response_schema: type[BaseModel],
        model_options: Mapping[str, JSONValue],
    ) -> StructuredResult:
        """Refuse discovery rather than silently rerouting to an incompatible fallback."""
        del messages, response_schema, model_options
        raise LLMWebSearchUnsupportedError()


def _parse_options(raw_config: str) -> tuple[ResearchSourceOptions | None, SourceError | None]:
    """Reject arbitrary source configuration before it can shape a model request."""
    try:
        return ResearchSourceOptions.model_validate(json.loads(raw_config)), None
    except (TypeError, ValueError, json.JSONDecodeError, ValidationError):
        return None, SourceError(
            code="INVALID_WEB_RESEARCH_CONFIG",
            summary="web_research source requires a valid fixed topic and query configuration",
            retryable=False,
        )


def _research_messages(
    options: ResearchSourceOptions, window: CollectionWindow, *, focus: str | None = None
) -> tuple[LLMMessage, ...]:
    """Build the stable discovery-only instruction; editorial prose is handled later."""
    start = window.start.astimezone(UTC).isoformat()
    end = window.end.astimezone(UTC).isoformat()
    facets = _research_facets(options.topic)
    return (
        LLMMessage(
            role="system",
            content=(
                "Use web search only to discover direct article candidates. "
                "Return structured JSON, not Markdown or a briefing. "
                "Prefer first-party primary sources. Never return search "
                "result pages, social posts, login pages, marketing pages, or undated old stories."
            ),
        ),
        LLMMessage(
            role="user",
            content=(
                (
                    f"主题：{options.query}\n"
                    f"时间窗口：{start} 至 {end}\n"
                    f"来源偏好：{options.publisher_preference}\n"
                    f"必须分别检索并逐项覆盖：{facets}。不要只围绕其中一个方向搜索。"
                )
                + (f"本轮重点检索：{focus}。\n" if focus is not None else "")
                + "只返回有可见发布日期的 HTML 新闻正文页；不返回 PDF、文档下载、"
                "搜索结果页、聚合页或需要登录的页面。"
                "在存在足够合格原文时，返回 12 至 20 条彼此不同的候选；"
                "只有确实没有可核验的直达原文时才少于该范围。"
                "每项提供标题、直达 URL、发布者、事实发现和发布时间提示。"
            ),
        ),
    )


def _research_facets(topic: Literal["telecom", "ai"]) -> str:
    """Keep one configured query while making the required search coverage explicit."""
    return "、".join(_RESEARCH_FACETS[topic])


def _research_call_facets(
    topic: Literal["telecom", "ai"], max_search_calls: int
) -> tuple[str, ...]:
    """Bound each source to the first configured management-relevant search facets."""
    return _RESEARCH_FACETS[topic][:max_search_calls]


def _candidate_url_error(raw_url: str) -> SourceError | None:
    """Reject obvious discovery surfaces before the safe fetcher handles network safety."""
    parsed = urlsplit(raw_url)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        return SourceError(
            code="INVALID_WEB_RESEARCH_URL",
            summary="web-search candidate URL must be an absolute HTTP(S) URL without credentials",
            retryable=False,
        )
    host = parsed.hostname.lower()
    if host in _READER_BLOCKED_HOSTS:
        return SourceError(
            code="READER_URL_BLOCKED",
            summary="web-search candidate domain is not reliable for reader-facing links",
            retryable=False,
        )
    if host in _DISCOVERY_HOSTS or parsed.path.lower().startswith("/search"):
        return SourceError(
            code="WEB_RESEARCH_DISCOVERY_PAGE",
            summary="web-search candidate URL points to a discovery page, not an article",
            retryable=False,
        )
    return None


__all__ = [
    "ResearchCollector",
    "UnavailableWebResearchProvider",
    "WebResearchCandidateSet",
]
