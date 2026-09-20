"""Zhipu BigModel web-search discovery for OpenAI-compatible GLM deployments.

Coding-plan keys cannot bill the REST ``/web_search`` route (HTTP 1113 when no
cash/resource pack remains). They can use the plan-native MCP server
``web_search_prime`` instead. This provider searches first and then asks the
model to filter those real results into the caller's candidate schema. URL
fidelity comes from the search backend, never from model transcription, and
every selected link still goes through the caller's independent
fetch-and-verify pipeline.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from dailycast.core.errors import (
    LLMProviderAuthenticationError,
    LLMProviderError,
    LLMProviderTimeoutError,
    LLMWebSearchUnsupportedError,
)
from dailycast.llm.contracts import JSONValue, LLMMessage, LLMUsage, StructuredResult
from dailycast.llm.providers.openai_compatible import (
    OpenAICompatibleLLMProvider,
    _with_json_object_contract,
)

logger = logging.getLogger(__name__)

_SEARCH_RESULT_COUNTS = {"low": 5, "medium": 10, "high": 20}
_SEARCH_RECENCY_FILTERS = frozenset({"oneDay", "oneWeek", "oneMonth", "oneYear", "noLimit"})
_MAX_SEARCH_QUERIES = 3
_SEARCH_ITEM_CONTENT_CHARS = 220
_CHAT_COMPLETIONS_SUFFIX = "/chat/completions"
_DEFAULT_MCP_SEARCH_ENDPOINT = "https://open.bigmodel.cn/api/mcp/web_search_prime/mcp"
_MCP_TOOL_NAME = "web_search_prime"
# Search endpoints rate-limit far more tightly than chat completions. Pace calls
# and back off long enough for a minute-scale quota window to reopen.
_SEARCH_CALL_INTERVAL_SECONDS = 0.6
_SEARCH_RETRY_BASE_SECONDS = 1.0
_SEARCH_RETRY_STEP_SECONDS = 2.0


@dataclass(frozen=True, slots=True)
class _SearchItem:
    """One verbatim search-API result; only bounded display fields are kept."""

    title: str
    link: str
    media: str
    publish_date: str
    snippet: str


class _GeneratedSearchQueries(BaseModel):
    """Bounded query list the model condenses from one research brief."""

    queries: list[str] = Field(min_length=1, max_length=6)


class ZhipuWebResearchProvider(OpenAICompatibleLLMProvider):
    """Discover candidates through Zhipu search (MCP or REST), then filter with the model."""

    provider_name = "zhipu_web_search"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        model: str,
        timeout_seconds: float,
        temperature: float,
        max_output_tokens: int | None = None,
        max_retries: int = 2,
        top_p: float | None = None,
        thinking: str | None = None,
        # search_pro multi-engine recall surfaces current news; search_std mostly
        # returns stale pages, which starves the time-windowed filter downstream.
        search_engine: str = "search_pro",
        search_recency_filter: str = "oneWeek",
        search_result_chars: int = 12_000,
        search_transport: Literal["mcp", "rest"] = "mcp",
        mcp_search_endpoint: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout_seconds=timeout_seconds,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            max_retries=max_retries,
            # Zhipu accepts wire-level json_schema but does not enforce it; the
            # prompt-embedded contract behind json_object mode is the verified path.
            response_format="json_object",
            top_p=top_p,
            thinking=thinking,
            http_client=http_client,
        )
        if not search_engine.strip() or search_result_chars <= 0:
            msg = "Zhipu web-research search engine and result budget must be valid"
            raise ValueError(msg)
        if search_recency_filter not in _SEARCH_RECENCY_FILTERS:
            msg = "Zhipu web-research search_recency_filter is not a supported upstream value"
            raise ValueError(msg)
        if search_transport not in {"mcp", "rest"}:
            msg = "Zhipu web-research search_transport must be mcp or rest"
            raise ValueError(msg)
        self._search_engine = search_engine
        self._search_recency_filter = search_recency_filter
        self._search_result_chars = search_result_chars
        self._search_transport = search_transport
        self._mcp_search_endpoint = (mcp_search_endpoint or _DEFAULT_MCP_SEARCH_ENDPOINT).strip()
        if not self._mcp_search_endpoint:
            msg = "Zhipu MCP search endpoint must be a non-empty URL"
            raise ValueError(msg)
        self._search_endpoint = (
            f"{self._endpoint.removesuffix(_CHAT_COMPLETIONS_SUFFIX)}/web_search"
        )

    async def generate_web_research(
        self,
        messages: Sequence[LLMMessage],
        response_schema: type[BaseModel],
        model_options: Mapping[str, JSONValue],
    ) -> StructuredResult:
        """Generate short queries, search them verbatim, then have the model filter the results.

        Long editorial facet descriptions retrieve mostly stale pages from the
        search API, so the model first condenses the research brief into short
        time-anchored queries; URL fidelity then comes from the search API and
        never from model transcription.
        """
        if not self._api_key:
            raise LLMProviderAuthenticationError()
        options = self._semantic_options(model_options)
        options.pop("response_format", None)
        context_size = options.pop("search_context_size", "medium")
        result_count = (
            _SEARCH_RESULT_COUNTS.get(context_size) if isinstance(context_size, str) else None
        )
        if result_count is None:
            msg = "web research search_context_size must be low, medium, or high"
            raise ValueError(msg)
        fallback_queries = _search_queries(options.pop("search_queries", None), messages)
        # Explicit collector facets are the preferred discovery shape: the search
        # API should see those queries verbatim. Generating extra chat queries on
        # top multiplies /web_search calls and trips Zhipu's search quota.
        if fallback_queries:
            queries = fallback_queries[:_MAX_SEARCH_QUERIES]
        else:
            generated = await self._generate_search_queries(messages, options)
            queries = generated[:_MAX_SEARCH_QUERIES]
            if not queries:
                queries = fallback_queries
        items = await self._collect_search_items(queries, result_count)
        if not items:
            return StructuredResult(
                content={"candidates": []},
                model=self.model,
                usage=LLMUsage(),
                request_id=None,
            )
        enriched = (
            *messages,
            LLMMessage(
                role="user", content=_search_context_message(items, self._search_result_chars)
            ),
        )
        payload: dict[str, object] = {
            "model": self.model,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in _with_json_object_contract(enriched, response_schema)
            ],
            "temperature": options.pop("temperature", self._temperature),
            "response_format": {"type": "json_object"},
        }
        max_output_tokens = options.pop("max_output_tokens", self.max_output_tokens)
        if max_output_tokens is not None:
            payload["max_tokens"] = max_output_tokens
        top_p = options.pop("top_p", self._top_p)
        if top_p is not None:
            payload["top_p"] = top_p
        if self._thinking is not None:
            payload["thinking"] = {"type": self._thinking}
        payload.update(options)
        response = await self._post(payload)
        return self._parse_response(response)

    async def _generate_search_queries(
        self, messages: Sequence[LLMMessage], options: Mapping[str, JSONValue]
    ) -> tuple[str, ...]:
        """Condense the research brief into bounded, time-anchored search queries."""
        brief = "\n".join(f"{message.role}: {message.content}" for message in messages)
        query_messages = (
            LLMMessage(role="system", content="你是新闻搜索查询优化器，只输出 JSON。"),
            LLMMessage(
                role="user",
                content=(
                    f"研究任务：\n{brief}\n\n"
                    "基于以上任务生成 4 至 6 条适合搜索引擎的简短中文查询词：每条不超过 16 个字，"
                    "覆盖不同主体、地域或技术角度，每条都必须包含时间敏感词"
                    "（如“最新”“发布”“开通”“动态”），不要输出完整句子。"
                ),
            ),
        )
        payload: dict[str, object] = {
            "model": self.model,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in _with_json_object_contract(query_messages, _GeneratedSearchQueries)
            ],
            "temperature": options.get("temperature", self._temperature),
            "response_format": {"type": "json_object"},
        }
        if self._thinking is not None:
            payload["thinking"] = {"type": self._thinking}
        response = await self._post(payload)
        try:
            generated = _GeneratedSearchQueries.model_validate(
                self._parse_response(response).content
            )
        except (ValueError, TypeError):
            return ()
        return tuple(query.strip() for query in generated.queries if query.strip())[
            :_MAX_SEARCH_QUERIES
        ]

    async def _collect_search_items(self, queries: Sequence[str], count: int) -> list[_SearchItem]:
        """Run queries one-by-one with pacing; merge results with case-insensitive deduplication."""
        items: list[_SearchItem] = []
        seen_links: set[str] = set()
        for index, query in enumerate(queries):
            if index:
                await asyncio.sleep(_SEARCH_CALL_INTERVAL_SECONDS)
            for item in await self._web_search(query, count):
                link_key = item.link.casefold()
                if link_key in seen_links:
                    continue
                seen_links.add(link_key)
                items.append(item)
        return items

    async def _web_search(self, query: str, count: int) -> list[_SearchItem]:
        """Dispatch to Coding Plan MCP or the REST /web_search endpoint."""
        if self._search_transport == "mcp":
            return await self._web_search_mcp(query, count)
        return await self._web_search_rest(query, count)

    async def _web_search_mcp(self, query: str, count: int) -> list[_SearchItem]:
        """Call Coding Plan's web_search_prime MCP tool once for a verbatim query."""
        if not self._api_key:
            raise LLMProviderAuthenticationError()
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        session_id = await self._mcp_initialize(headers)
        if session_id:
            headers["Mcp-Session-Id"] = session_id
            await self._mcp_notify_initialized(headers)
        payload = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": _MCP_TOOL_NAME,
                "arguments": {
                    "search_query": query[:70],
                    "search_recency_filter": self._search_recency_filter,
                    "content_size": "medium",
                    "location": "cn",
                },
            },
        }
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.post(
                    self._mcp_search_endpoint,
                    headers=headers,
                    json=payload,
                    timeout=self._timeout_seconds,
                )
            except httpx.TimeoutException as error:
                if attempt < self._max_retries:
                    await asyncio.sleep(_search_retry_delay(attempt))
                    continue
                raise LLMProviderTimeoutError() from error
            except httpx.RequestError as error:
                if attempt < self._max_retries:
                    await asyncio.sleep(_search_retry_delay(attempt))
                    continue
                raise LLMProviderError() from error
            if response.status_code in {429, 500, 502, 503, 504} and attempt < self._max_retries:
                await asyncio.sleep(_search_retry_delay(attempt))
                continue
            if response.status_code in {400, 422}:
                raise LLMWebSearchUnsupportedError()
            if response.status_code in {401, 403}:
                raise LLMProviderAuthenticationError()
            if response.is_error:
                _log_zhipu_error_body("mcp_search", response)
                raise LLMProviderError()
            items = _parse_mcp_search_items(response)
            return items[: max(1, count)]
        raise LLMProviderError()

    async def _mcp_initialize(self, headers: Mapping[str, str]) -> str | None:
        """Open one MCP session and return its session id when the server provides one."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "dailycast-briefing", "version": "0.1.0"},
            },
        }
        try:
            response = await self._client.post(
                self._mcp_search_endpoint,
                headers=dict(headers),
                json=payload,
                timeout=self._timeout_seconds,
            )
        except httpx.TimeoutException as error:
            raise LLMProviderTimeoutError() from error
        except httpx.RequestError as error:
            raise LLMProviderError() from error
        if response.status_code in {401, 403}:
            raise LLMProviderAuthenticationError()
        if response.status_code in {400, 422}:
            raise LLMWebSearchUnsupportedError()
        if response.is_error:
            _log_zhipu_error_body("mcp_initialize", response)
            raise LLMProviderError()
        body = _parse_mcp_jsonrpc(response)
        if isinstance(body, dict) and "error" in body:
            rpc_error = body.get("error")
            message = rpc_error.get("message") if isinstance(rpc_error, dict) else str(rpc_error)
            if message and "not found" in str(message).lower():
                raise LLMWebSearchUnsupportedError()
            logger.warning(
                "zhipu mcp initialize returned a json-rpc error",
                extra={"message": str(message)[:200]},
            )
        session_id = response.headers.get("Mcp-Session-Id") or response.headers.get(
            "mcp-session-id"
        )
        return session_id or None

    async def _mcp_notify_initialized(self, headers: Mapping[str, str]) -> None:
        """Best-effort initialized notification required by streamable HTTP MCP."""
        payload = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        try:
            await self._client.post(
                self._mcp_search_endpoint,
                headers=dict(headers),
                json=payload,
                timeout=self._timeout_seconds,
            )
        except (httpx.TimeoutException, httpx.RequestError):
            logger.debug("zhipu mcp initialized notification failed; continuing")

    async def _web_search_rest(self, query: str, count: int) -> list[_SearchItem]:
        """Call the verbatim-URL REST search endpoint with rate-limit-aware retry backoff."""
        headers = {"Authorization": f"Bearer {self._api_key}"}
        payload = {
            "search_engine": self._search_engine,
            "search_query": query,
            "count": count,
            "search_recency_filter": self._search_recency_filter,
        }
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.post(
                    self._search_endpoint,
                    headers=headers,
                    json=payload,
                    timeout=self._timeout_seconds,
                )
            except httpx.TimeoutException as error:
                if attempt < self._max_retries:
                    await asyncio.sleep(_search_retry_delay(attempt))
                    continue
                raise LLMProviderTimeoutError() from error
            except httpx.RequestError as error:
                if attempt < self._max_retries:
                    await asyncio.sleep(_search_retry_delay(attempt))
                    continue
                raise LLMProviderError() from error
            if response.status_code in {429, 500, 502, 503, 504} and attempt < self._max_retries:
                _log_zhipu_error_body("rest_search", response)
                await asyncio.sleep(_search_retry_delay(attempt))
                continue
            if response.status_code in {400, 422}:
                raise LLMWebSearchUnsupportedError()
            if response.status_code in {401, 403}:
                raise LLMProviderAuthenticationError()
            if response.is_error:
                _log_zhipu_error_body("rest_search", response)
                raise LLMProviderError()
            return _parse_search_items(response)
        raise LLMProviderError()


def _search_retry_delay(attempt: int) -> float:
    """Grow waits across attempts so 429 can clear a real provider rate window."""
    return _SEARCH_RETRY_BASE_SECONDS + _SEARCH_RETRY_STEP_SECONDS * attempt


def _log_zhipu_error_body(stage: str, response: httpx.Response) -> None:
    """Record the provider business error code so 429 is diagnosable (e.g. 1113)."""
    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text[:200]}
    error = body.get("error") if isinstance(body, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    logger.warning(
        "zhipu search backend error",
        extra={
            "stage": stage,
            "http_status": response.status_code,
            "error_code": code,
            "error_message": message,
        },
    )


def _parse_mcp_jsonrpc(response: httpx.Response) -> dict[str, Any]:
    """Parse streamable-HTTP MCP payloads, including SSE `data:` frames."""
    text = response.text
    if "data:" in text:
        for line in text.splitlines():
            if line.startswith("data:"):
                candidate = line[5:].strip()
                try:
                    parsed = json.loads(candidate)
                except ValueError:
                    continue
                if isinstance(parsed, dict):
                    return parsed
    try:
        parsed = json.loads(text)
    except ValueError as error:
        raise LLMProviderError() from error
    if not isinstance(parsed, dict):
        raise LLMProviderError()
    return parsed


def _parse_mcp_search_items(response: httpx.Response) -> list[_SearchItem]:
    """Map MCP web_search_prime tool output into the shared search-item shape."""
    body = _parse_mcp_jsonrpc(response)
    if "error" in body and "result" not in body:
        error = body.get("error")
        message = str(error.get("message")) if isinstance(error, dict) else str(error)
        if "not found" in message.lower():
            raise LLMWebSearchUnsupportedError()
        raise LLMProviderError()
    result = body.get("result")
    if not isinstance(result, dict):
        raise LLMProviderError()
    content = result.get("content")
    if not isinstance(content, list) or not content:
        return []
    first = content[0]
    text = first.get("text") if isinstance(first, dict) else None
    if not isinstance(text, str) or not text.strip():
        return []
    rows = _load_json_rows(text)
    items: list[_SearchItem] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = row.get("title")
        link = row.get("link")
        if not isinstance(title, str) or not title.strip():
            continue
        if not isinstance(link, str) or not link.strip():
            continue
        media = row.get("media")
        publish_date = row.get("publish_date")
        items.append(
            _SearchItem(
                title=title.strip(),
                link=link.strip(),
                media=media.strip() if isinstance(media, str) and media.strip() else "未知",
                publish_date=publish_date.strip() if isinstance(publish_date, str) else "",
                snippet=_bounded_snippet(row.get("content")),
            )
        )
    return items


def _load_json_rows(text: str) -> list[object]:
    """Decode MCP tool text that may itself be a JSON string of result rows."""
    payload: object = text
    for _ in range(2):
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                return []
            continue
        break
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        rows = payload.get("search_result")
        if isinstance(rows, list):
            return rows
        candidates = payload.get("candidates")
        if isinstance(candidates, list):
            return candidates
    return []


def _parse_search_items(response: httpx.Response) -> list[_SearchItem]:
    """Keep only fully populated verbatim result rows; usage fields are absent here."""
    try:
        body = response.json()
    except ValueError as error:
        raise LLMProviderError() from error
    rows = body.get("search_result") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        raise LLMProviderError()
    items: list[_SearchItem] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title, link = row.get("title"), row.get("link")
        media, publish_date = row.get("media"), row.get("publish_date")
        if not isinstance(title, str) or not title.strip():
            continue
        if not isinstance(link, str) or not link.strip():
            continue
        items.append(
            _SearchItem(
                title=title.strip(),
                link=link.strip(),
                media=media.strip() if isinstance(media, str) and media.strip() else "未知",
                publish_date=publish_date.strip() if isinstance(publish_date, str) else "",
                snippet=_bounded_snippet(row.get("content")),
            )
        )
    return items


def _bounded_snippet(content: object) -> str:
    """Collapse one search-result excerpt to a single bounded display line."""
    if not isinstance(content, str):
        return ""
    return " ".join(content.split())[:_SEARCH_ITEM_CONTENT_CHARS]


def _search_context_message(items: Sequence[_SearchItem], max_chars: int) -> str:
    """Render verbatim search rows and forbid any URL rewriting in the filter step."""
    header = (
        "以下是搜索接口返回的真实候选结果，每行格式：标题 | 链接 | 媒体 | 发布日期 | 摘要。\n"
        "只能从这些链接中选择候选；链接必须逐字保留，禁止改写、截断或自行构造，"
        "并把行内发布日期填入 published_at_hint。\n"
        "结合前文的时间窗与主题要求筛选：排除首页、频道页、聚合页、登录页、"
        "无发布日期或发布日期明显超出时间窗的条目。\n"
    )
    lines: list[str] = []
    included_chars = len(header)
    dropped = 0
    for item in items:
        line = (
            f"{item.title} | {item.link} | {item.media} | "
            f"{item.publish_date or '未知'} | {item.snippet}"
        )
        if included_chars + len(line) > max_chars:
            dropped += 1
            continue
        included_chars += len(line)
        lines.append(line)
    body = "\n".join(lines)
    suffix = f"\n（另有 {dropped} 条结果因长度限制被省略。）" if dropped else ""
    return header + body + suffix


def _search_queries(raw: JSONValue | None, messages: Sequence[LLMMessage]) -> tuple[str, ...]:
    """Prefer explicit collector queries and fall back to the last user instruction."""
    texts: list[str] = []
    if isinstance(raw, (list, tuple)):
        texts.extend(query.strip() for query in raw if isinstance(query, str) and query.strip())
    if not texts:
        last_user = next(
            (message.content for message in reversed(messages) if message.role == "user"),
            "",
        )
        if last_user.strip():
            texts.append(last_user.strip())
    return tuple(texts[:_MAX_SEARCH_QUERIES])


__all__ = ["ZhipuWebResearchProvider"]
