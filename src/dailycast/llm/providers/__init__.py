"""Direct LLM provider implementations for configured external model endpoints."""

from dailycast.llm.providers.failover import FailoverLLMProvider

__all__ = ["FailoverLLMProvider"]
