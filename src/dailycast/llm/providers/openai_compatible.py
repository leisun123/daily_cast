"""OpenAI-compatible structured JSON provider with safe configuration identity."""

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
_JSON_OBJECT_CONTRACT_VERSION = "chat-completions-json-object-schema-v1"


def _canonical_json(value: object) -> str:
    """Return a stable JSON representation used only to hash safe semantic configuration."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class OpenAICompatibleLLMProvider:
    """Call a configured OpenAI-compatible chat-completions endpoint with JSON schema output."""

    provider_name = "openai_compatible"

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
        """Hash only output-semantic settings and a credential-free endpoint identity."""
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
            "temperature": options.pop("temperature", self._temperature),
            "top_p_or_null": options.pop("top_p", self._top_p),
        }
        return sha256_text(_canonical_json(payload))

    async def ping(self) -> None:
        """Confirm the endpoint answers with valid credentials without generating."""
        response = await self._client.get(
            f"{self._endpoint[: -len('/chat/completions')]}/models",
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=10.0,
        )
        if response.status_code in (401, 403):
            raise LLMProviderAuthenticationError
        if response.status_code >= 400:
            raise LLMProviderError

    async def generate_structured(
        self,
        operation: LLMOperation,
        messages: Sequence[LLMMessage],
        response_schema: type[BaseModel],
        model_options: Mapping[str, JSONValue],
    ) -> StructuredResult:
        """Submit a strict JSON-schema request and map safe provider outcomes to typed errors."""
        del operation
        if not self._api_key:
            raise LLMProviderAuthenticationError()
        options = self._semantic_options(model_options)
        response_mode = options.pop("response_format", self._response_format)
        request_messages = (
            _with_json_object_contract(messages, response_schema)
            if response_mode == "json_object"
            else messages
        )
        payload: dict[str, object] = {
            "model": self.model,
            "messages": [
                {"role": message.role, "content": message.content} for message in request_messages
            ],
            "temperature": options.pop("temperature", self._temperature),
        }
        max_output_tokens = options.pop("max_output_tokens", self.max_output_tokens)
        if max_output_tokens is not None:
            payload["max_tokens"] = max_output_tokens
        top_p = options.pop("top_p", self._top_p)
        if top_p is not None:
            payload["top_p"] = top_p
        payload["response_format"] = self._response_format_payload(response_mode, response_schema)
        payload.update(options)
        response = await self._post(payload)
        return self._parse_response(response)

    def _semantic_options(self, model_options: Mapping[str, JSONValue]) -> dict[str, JSONValue]:
        """Retain actual model semantics while excluding keys and transport-only options."""
        return {
            key: value
            for key, value in model_options.items()
            if key.lower() not in _NON_SEMANTIC_OPTIONS
        }

    def _response_format_payload(
        self, response_mode: JSONValue, response_schema: type[BaseModel]
    ) -> dict[str, object]:
        """Build the documented JSON-schema request, with JSON-object compatibility fallback."""
        if response_mode == "json_object":
            return {"type": "json_object"}
        if response_mode != "json_schema":
            msg = "unsupported structured response format"
            raise ValueError(msg)
        schema_name = response_schema.__name__.lower().replace("_", "-")
        return {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "schema": response_schema.model_json_schema(),
                "strict": True,
            },
        }

    async def _post(self, payload: Mapping[str, object]) -> httpx.Response:
        """Perform bounded retry for transient safe-to-retry transport or server failures."""
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
            if response.is_error:
                raise LLMProviderError()
            return response
        raise LLMProviderError()

    def _parse_response(self, response: httpx.Response) -> StructuredResult:
        """Decode only the documented bounded result fields, never logging provider payloads."""
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
        """Normalize endpoint identity and reject credential-bearing URL forms before use."""
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
                f"{path}/chat/completions",
                normalized_query,
                "",
            )
        )
        return endpoint, sha256_text(identity_url)


def _response_content(body: Mapping[str, object]) -> dict[str, JSONValue]:
    """Extract one object-valued JSON message from an OpenAI-compatible completion body."""
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMProviderResponseError()
    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        raise LLMProviderResponseError()
    message = first_choice.get("message")
    if not isinstance(message, dict):
        raise LLMProviderResponseError()
    raw_content = message.get("content")
    if isinstance(raw_content, str):
        try:
            parsed = json.loads(raw_content)
        except json.JSONDecodeError as error:
            raise LLMProviderResponseError() from error
    else:
        parsed = raw_content
    if not isinstance(parsed, dict):
        raise LLMProviderResponseError()
    return parsed


def _with_json_object_contract(
    messages: Sequence[LLMMessage], response_schema: type[BaseModel]
) -> tuple[LLMMessage, ...]:
    """Embed the local schema when a compatible endpoint only exposes JSON-object mode."""
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


def _response_usage(body: Mapping[str, object]) -> LLMUsage:
    """Map optional provider usage fields to safe non-negative counters."""
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return LLMUsage()
    return LLMUsage(
        input_tokens=_nonnegative_int(usage.get("prompt_tokens")),
        output_tokens=_nonnegative_int(usage.get("completion_tokens")),
    )


def _nonnegative_int(value: object) -> int:
    """Treat absent or malformed usage as zero instead of storing invalid database values."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value
