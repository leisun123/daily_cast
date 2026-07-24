"""Typed contracts shared by structured LLM providers and artifact caching."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel

from dailycast.core.errors import LLMProviderTimeoutError
from dailycast.db.models import LLMOperation

type JSONValue = str | int | float | bool | None | list["JSONValue"] | dict[str, "JSONValue"]


@dataclass(frozen=True, slots=True)
class LLMMessage:
    """One role-labelled message sent to a structured LLM provider."""

    role: str
    content: str


@dataclass(frozen=True, slots=True)
class LLMUsage:
    """Provider-reported token usage for one actual model invocation."""

    input_tokens: int = 0
    output_tokens: int = 0

    def __post_init__(self) -> None:
        """Reject impossible usage before it can be written to SQLite."""
        if self.input_tokens < 0 or self.output_tokens < 0:
            msg = "LLM usage tokens must be non-negative"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class StructuredResult:
    """A locally validated structured LLM result or reused artifact payload."""

    content: Mapping[str, JSONValue]
    model: str
    usage: LLMUsage
    request_id: str | None
    cache_hit: bool = False
    artifact_id: int | None = None
    provider_call_count: int = 1


class LLMProvider(Protocol):
    """A direct model endpoint that can produce one structured JSON response."""

    provider_name: str
    model: str
    max_output_tokens: int

    def generation_config_hash(self, model_options: Mapping[str, JSONValue]) -> str:
        """Return the non-secret cache identity for semantic generation settings."""

    async def generate_structured(
        self,
        operation: LLMOperation,
        messages: Sequence[LLMMessage],
        response_schema: type[BaseModel],
        model_options: Mapping[str, JSONValue],
    ) -> StructuredResult:
        """Request provider JSON output without deciding any editorial workflow."""


__all__ = [
    "JSONValue",
    "LLMMessage",
    "LLMProvider",
    "LLMProviderTimeoutError",
    "LLMUsage",
    "StructuredResult",
]
