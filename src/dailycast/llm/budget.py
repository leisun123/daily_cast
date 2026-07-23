"""Conservative, in-memory per-task LLM budget reservation."""

from __future__ import annotations

import math
from collections.abc import Sequence

from dailycast.core.errors import AIBudgetExceededError
from dailycast.llm.contracts import LLMMessage


class BudgetController:
    """Reserve a bounded call and token allowance before a cache-miss model call."""

    def __init__(
        self,
        *,
        max_calls: int = 12,
        max_input_tokens: int = 60_000,
        max_output_tokens: int = 15_000,
    ) -> None:
        if min(max_calls, max_input_tokens, max_output_tokens) < 0:
            msg = "LLM budget limits must be non-negative"
            raise ValueError(msg)
        self._max_calls = max_calls
        self._max_input_tokens = max_input_tokens
        self._max_output_tokens = max_output_tokens
        self.call_count = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def reserve(self, *, input_tokens: int, output_tokens: int) -> None:
        """Fail before a provider call when its conservative allowance exceeds a limit."""
        if input_tokens < 0 or output_tokens < 0:
            msg = "LLM token reservations must be non-negative"
            raise ValueError(msg)
        if (
            self.call_count + 1 > self._max_calls
            or self.input_tokens + input_tokens > self._max_input_tokens
            or self.output_tokens + output_tokens > self._max_output_tokens
        ):
            raise AIBudgetExceededError()
        self.call_count += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens


def estimate_message_input_tokens(messages: Sequence[LLMMessage]) -> int:
    """Conservatively estimate tokens for budget reservation without persisting prompt text."""
    encoded_bytes = sum(
        len(message.role.encode("utf-8")) + len(message.content.encode("utf-8"))
        for message in messages
    )
    return max(1, math.ceil(encoded_bytes / 3) + (8 * len(messages)))
