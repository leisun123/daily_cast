"""OpenAI Responses API provider for Codex-compatible model gateways."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from pydantic import BaseModel

from dailycast.core.errors import (
    LLMProviderAuthenticationError,
    LLMProviderError,
    LLMProviderResponseError,
    LLMProviderTimeoutError,
    LLMStructuredOutputUnsupportedError,
    LLMWebSearchUnsupportedError,
)
from dailycast.core.hashes import sha256_text
from dailycast.db.models import LLMOperation
from dailycast.llm.contracts import JSONValue, LLMMessage, LLMUsage, StructuredResult

_NON_SEMANTIC_OPTIONS = frozenset(
    {
        "api_key",
        "authorization",
        "base_url",
        "max_retries",
        "retry_count",
        "timeout",
        "timeout_seconds",
    }
)
_SENSITIVE_QUERY_MARKERS = ("api_key", "auth", "credential", "key", "secret", "signature", "token")
_JSON_OBJECT_CONTRACT_VERSION = "responses-json-object-schema-v1"


def _canonical_json(value: object) -> str:
    """Return a stable JSON representation used only for non-secret cache identity."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class OpenAIResponsesLLMProvider:
    """Call an OpenAI Responses-compatible endpoint and extract one JSON object output."""

    provider_name = "openai_responses"
    supports_json_object_fallback = True

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
        response_format: str = "json_schema",
        top_p: float | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if (
            timeout_seconds <= 0
            or (max_output_tokens is not None and max_output_tokens < 0)
            or max_retries < 0
        ):
            msg = "LLM provider timeout, token limit, and retry count must be valid"
            raise ValueError(msg)
        self._endpoint, self._endpoint_identity_hash = self._endpoint_details(base_url)
        self._api_key = api_key
        self.model = model
        self.max_output_tokens = max_output_tokens
        self._timeout_seconds = timeout_seconds
        self._temperature = temperature
        self._max_retries = max_retries
        self._response_format = response_format
        self._top_p = top_p
        self._client = http_client or httpx.AsyncClient()

    def generation_config_hash(self, model_options: Mapping[str, JSONValue]) -> str:
        """Hash only output-semantic configuration and a credential-free endpoint identity."""
        options = self._semantic_options(model_options)
        response_format = options.pop("response_format", self._response_format)
        payload = {
            "endpoint_identity_hash": self._endpoint_identity_hash,
            "max_output_tokens": options.pop("max_output_tokens", self.max_output_tokens),
            "provider_model_options_sorted": options,
            "response_format_or_structured_output_mode": response_format,
            "json_object_contract_version": (
                _JSON_OBJECT_CONTRACT_VERSION if response_format == "json_object" else None
            ),
            "json_schema_fallback_contract_version": (
                _JSON_OBJECT_CONTRACT_VERSION if response_format == "json_schema" else None
            ),
            "temperature": options.pop("temperature", self._temperature),
            "top_p_or_null": options.pop("top_p", self._top_p),
            "wire_api": "responses",
        }
        return sha256_text(_canonical_json(payload))

    async def ping(self) -> None:
        """Exercise the real generation path with one minimal response.

        The models list can stay green while the upstream model service is
        down (a gateway answers metadata itself), so health means one tiny
        generation returning 2xx. No output budget is imposed and the body
        is not validated: the model manages its own reasoning, and any
        successful completion still proves the path works. Transient
        transport hiccups and 5xx answers get one retry so a blip does not
        page the operator; deterministic failures (4xx) report immediately.
        """
        for attempt in (1, 2):
            try:
                response = await self._client.post(
                    self._endpoint,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={"model": self.model, "input": "Reply with the word: ok"},
                    timeout=60.0,
                )
            except httpx.TimeoutException as error:
                if attempt == 2:
                    raise LLMProviderTimeoutError from error
                await asyncio.sleep(2.0)
                continue
            except httpx.TransportError as error:
                if attempt == 2:
                    raise LLMProviderError from error
                await asyncio.sleep(2.0)
                continue
            if response.status_code < 400:
                return
            if response.status_code in (401, 403):
                raise LLMProviderAuthenticationError
            if attempt == 2:
                raise LLMProviderError
            await asyncio.sleep(2.0)

    async def generate_structured(
        self,
        operation: LLMOperation,
        messages: Sequence[LLMMessage],
        response_schema: type[BaseModel],
        model_options: Mapping[str, JSONValue],
    ) -> StructuredResult:
        """Request one Responses API JSON result while leaving editorial logic to callers."""
        del operation
        if not self._api_key:
            raise LLMProviderAuthenticationError()
        options = self._semantic_options(model_options)
        response_mode = options.pop("response_format", self._response_format)
        payload = self._request_payload(messages, response_schema, options, response_mode)
        response = await self._post(payload, structured_output=response_mode == "json_schema")
        return self._parse_response(response)

    async def generate_web_research(
        self,
        messages: Sequence[LLMMessage],
        response_schema: type[BaseModel],
        model_options: Mapping[str, JSONValue],
    ) -> StructuredResult:
        """Discover structured candidates through the native Responses web-search tool."""
        if not self._api_key:
            raise LLMProviderAuthenticationError()
        options = self._semantic_options(model_options)
        response_mode = options.pop("response_format", self._response_format)
        search_context_size = options.pop("search_context_size", "medium")
        if search_context_size not in {"low", "medium", "high"}:
            msg = "web research search_context_size must be low, medium, or high"
            raise ValueError(msg)
        payload = self._request_payload(messages, response_schema, options, response_mode)
        # Some OpenAI-compatible gateways support native web_search but reject a
        # simultaneous Responses text.format request. JSON-object mode already
        # embeds the schema contract in the developer prompt and is validated
        # locally in _parse_response, so omit only this incompatible wire hint.
        payload.pop("text", None)
        payload["tools"] = [{"type": "web_search", "search_context_size": search_context_size}]
        # Discovery is meaningless when the model elects to answer from memory;
        # require at least one native web-search invocation for every research run.
        payload["tool_choice"] = "required"
        response = await self._post(
            payload,
            structured_output=response_mode == "json_schema",
            web_search=True,
        )
        return self._parse_response(response)

    def _request_payload(
        self,
        messages: Sequence[LLMMessage],
        response_schema: type[BaseModel],
        options: Mapping[str, JSONValue],
        response_mode: JSONValue,
    ) -> dict[str, object]:
        """Build one protocol payload, adding a local schema contract only for JSON-object mode."""
        request_options = dict(options)
        request_messages = (
            _with_json_object_contract(messages, response_schema)
            if response_mode == "json_object"
            else messages
        )
        payload: dict[str, object] = {
            "model": self.model,
            "input": _responses_input(request_messages),
            "temperature": request_options.pop("temperature", self._temperature),
            "text": {"format": self._response_format_payload(response_mode, response_schema)},
        }
        max_output_tokens = request_options.pop("max_output_tokens", self.max_output_tokens)
        if max_output_tokens is not None:
            payload["max_output_tokens"] = max_output_tokens
        top_p = request_options.pop("top_p", self._top_p)
        if top_p is not None:
            payload["top_p"] = top_p
        payload.update(request_options)
        return payload

    def _semantic_options(self, model_options: Mapping[str, JSONValue]) -> dict[str, JSONValue]:
        """Keep output-affecting options while excluding credentials and transport controls."""
        return {
            key: value
            for key, value in model_options.items()
            if key.lower() not in _NON_SEMANTIC_OPTIONS
        }

    def _response_format_payload(
        self, response_mode: JSONValue, response_schema: type[BaseModel]
    ) -> dict[str, object]:
        """Build the Responses API structured-output format object."""
        if response_mode == "json_object":
            return {"type": "json_object"}
        if response_mode != "json_schema":
            msg = "unsupported structured response format"
            raise ValueError(msg)
        schema_name = response_schema.__name__.lower().replace("_", "-")
        return {
            "type": "json_schema",
            "name": schema_name,
            "schema": response_schema.model_json_schema(),
            "strict": True,
        }

    async def _post(
        self,
        payload: Mapping[str, object],
        *,
        structured_output: bool = False,
        web_search: bool = False,
    ) -> httpx.Response:
        """Perform bounded retry for transient transport and server failures."""
        headers = {"Authorization": f"Bearer {self._api_key}"}
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.post(
                    self._endpoint,
                    headers=headers,
                    json=payload,
                    timeout=self._timeout_seconds,
                )
            except httpx.TimeoutException as error:
                if attempt < self._max_retries:
                    await asyncio.sleep(0.1 * (attempt + 1))
                    continue
                raise LLMProviderTimeoutError() from error
            except httpx.RequestError as error:
                if attempt < self._max_retries:
                    await asyncio.sleep(0.1 * (attempt + 1))
                    continue
                raise LLMProviderError() from error
            if response.status_code in {429, 500, 502, 503, 504} and attempt < self._max_retries:
                await asyncio.sleep(0.1 * (attempt + 1))
                continue
            if response.status_code in {401, 403}:
                raise LLMProviderAuthenticationError()
            if web_search and response.status_code in {400, 422}:
                raise LLMWebSearchUnsupportedError()
            if structured_output and response.status_code in {400, 422}:
                raise LLMStructuredOutputUnsupportedError()
            if response.is_error:
                raise LLMProviderError()
            return response
        raise LLMProviderError()

    def _parse_response(self, response: httpx.Response) -> StructuredResult:
        """Parse a completed Responses payload without logging provider response bodies."""
        try:
            body = response.json()
        except ValueError as error:
            raise LLMProviderResponseError() from error
        if not isinstance(body, dict):
            raise LLMProviderResponseError()
        content = _response_content(body)
        usage = _response_usage(body)
        request_id = response.headers.get("x-request-id")
        body_request_id = body.get("id")
        if request_id is None and isinstance(body_request_id, str):
            request_id = body_request_id
        return StructuredResult(
            content=content,
            model=self.model,
            usage=usage,
            request_id=request_id,
        )

    @staticmethod
    def _endpoint_details(base_url: str) -> tuple[str, str]:
        """Normalize the endpoint and reject a URL that may embed credentials."""
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            msg = "LLM base_url must be an absolute HTTP or HTTPS URL"
            raise ValueError(msg)
        if parsed.username is not None or parsed.password is not None:
            msg = "LLM base_url must not contain userinfo"
            raise ValueError(msg)
        query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
        if any(
            marker in key.lower() for key, _ in query_pairs for marker in _SENSITIVE_QUERY_MARKERS
        ):
            msg = "LLM base_url query must not contain credential-like keys"
            raise ValueError(msg)
        try:
            port = parsed.port
        except ValueError as error:
            msg = "LLM base_url has an invalid port"
            raise ValueError(msg) from error
        host = parsed.hostname.lower()
        netloc = host
        if port is not None and (parsed.scheme, port) not in {("http", 80), ("https", 443)}:
            netloc = f"{host}:{port}"
        path = parsed.path.rstrip("/")
        normalized_query = urlencode(sorted(query_pairs))
        identity_url = urlunsplit((parsed.scheme.lower(), netloc, path, normalized_query, ""))
        endpoint = urlunsplit(
            (
                parsed.scheme.lower(),
                netloc,
                f"{path}/responses",
                normalized_query,
                "",
            )
        )
        return endpoint, sha256_text(identity_url)


def _responses_input(messages: Sequence[LLMMessage]) -> list[dict[str, object]]:
    """Map existing system/user messages to the documented Responses input item shape."""
    input_items: list[dict[str, object]] = []
    for message in messages:
        role = "developer" if message.role == "system" else message.role
        input_items.append(
            {
                "role": role,
                "content": [{"type": "input_text", "text": message.content}],
            }
        )
    return input_items


def _with_json_object_contract(
    messages: Sequence[LLMMessage], response_schema: type[BaseModel]
) -> tuple[LLMMessage, ...]:
    """Embed a versioned local schema contract when a gateway supports JSON mode only."""
    schema_json = _canonical_json(response_schema.model_json_schema())
    contract = (
        "Structured output contract: return exactly one JSON object that conforms to this JSON "
        "Schema. Do not use Markdown, explanations, or additional top-level keys. JSON Schema: "
        f"{schema_json}"
    )
    enriched: list[LLMMessage] = []
    attached = False
    for message in messages:
        if message.role == "system" and not attached:
            enriched.append(LLMMessage(role="system", content=f"{message.content}\n\n{contract}"))
            attached = True
        else:
            enriched.append(message)
    if not attached:
        enriched.insert(0, LLMMessage(role="system", content=contract))
    return tuple(enriched)


def _response_content(body: Mapping[str, object]) -> dict[str, JSONValue]:
    """Read output_text items only and require their combined value to be one JSON object."""
    output = body.get("output")
    if not isinstance(output, list):
        raise LLMProviderResponseError()
    fragments: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "output_text":
                continue
            text = part.get("text")
            if isinstance(text, str):
                fragments.append(text)
    if not fragments:
        raise LLMProviderResponseError()
    try:
        parsed = json.loads("".join(fragments))
    except json.JSONDecodeError as error:
        raise LLMProviderResponseError() from error
    if not isinstance(parsed, dict):
        raise LLMProviderResponseError()
    return parsed


def _response_usage(body: Mapping[str, object]) -> LLMUsage:
    """Map Responses usage fields to safe non-negative counters."""
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return LLMUsage()
    return LLMUsage(
        input_tokens=_nonnegative_int(usage.get("input_tokens")),
        output_tokens=_nonnegative_int(usage.get("output_tokens")),
    )


def _nonnegative_int(value: object) -> int:
    """Treat absent or malformed usage counters as zero."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value
