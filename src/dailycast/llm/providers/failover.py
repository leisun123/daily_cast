"""Ordered LLM provider failover without leaking routing into editorial workflows."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import BaseModel

from dailycast.core.errors import LLMProviderError
from dailycast.db.models import LLMOperation
from dailycast.llm.contracts import JSONValue, LLMMessage, LLMProvider, StructuredResult


class FailoverLLMProvider:
    """Prefer the first provider and walk the ordered fallbacks after provider failures.

    One provider-level failure — transport, auth, quota exhaustion, or malformed
    responses — advances to the next configured provider exactly once per logical
    call; the chain never retries an already-failed provider within one call.
    """

    def __init__(self, primary: LLMProvider, fallbacks: Sequence[LLMProvider]) -> None:
        if not fallbacks:
            msg = "FailoverLLMProvider requires at least one fallback provider"
            raise ValueError(msg)
        self.providers = (primary, *fallbacks)
        self.provider_name = primary.provider_name
        self.model = primary.model
        self.max_output_tokens = primary.max_output_tokens

    @property
    def primary(self) -> LLMProvider:
        """Expose the preferred provider for per-attempt wrappers without rerouting."""
        return self.providers[0]

    @property
    def fallback(self) -> LLMProvider:
        """Expose the first secondary provider for per-attempt wrappers."""
        return self.providers[1]

    def generation_config_hash(self, model_options: Mapping[str, JSONValue]) -> str:
        """Expose the preferred provider identity to direct protocol consumers."""
        return self.providers[0].generation_config_hash(model_options)

    async def generate_structured(
        self,
        operation: LLMOperation,
        messages: Sequence[LLMMessage],
        response_schema: type[BaseModel],
        model_options: Mapping[str, JSONValue],
    ) -> StructuredResult:
        """Try providers in order until one succeeds or every provider has failed."""
        last_error: LLMProviderError | None = None
        for index, provider in enumerate(self.providers):
            try:
                result = await provider.generate_structured(
                    operation, messages, response_schema, model_options
                )
            except LLMProviderError as error:
                last_error = error
                continue
            if index == 0:
                return result
            return StructuredResult(
                content=result.content,
                model=result.model,
                usage=result.usage,
                request_id=result.request_id,
                cache_hit=result.cache_hit,
                artifact_id=result.artifact_id,
                provider_call_count=result.provider_call_count + index,
            )
        assert last_error is not None
        raise last_error


__all__ = ["FailoverLLMProvider"]
