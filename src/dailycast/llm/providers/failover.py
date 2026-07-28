"""Ordered LLM provider failover without leaking routing into editorial workflows."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import BaseModel

from dailycast.core.errors import LLMProviderError
from dailycast.db.models import LLMOperation
from dailycast.llm.contracts import JSONValue, LLMMessage, LLMProvider, StructuredResult


class FailoverLLMProvider:
    """Prefer the first provider and use the second only after a provider-level failure."""

    def __init__(self, primary: LLMProvider, fallback: LLMProvider) -> None:
        self.providers = (primary, fallback)
        self.provider_name = primary.provider_name
        self.model = primary.model
        self.max_output_tokens = primary.max_output_tokens

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
        """Try the primary once, then route a provider failure to the configured fallback."""
        try:
            return await self.providers[0].generate_structured(
                operation, messages, response_schema, model_options
            )
        except LLMProviderError:
            result = await self.providers[1].generate_structured(
                operation, messages, response_schema, model_options
            )
            return StructuredResult(
                content=result.content,
                model=result.model,
                usage=result.usage,
                request_id=result.request_id,
                cache_hit=result.cache_hit,
                artifact_id=result.artifact_id,
                provider_call_count=result.provider_call_count + 1,
            )


__all__ = ["FailoverLLMProvider"]
