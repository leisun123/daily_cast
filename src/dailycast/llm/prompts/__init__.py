"""Versioned prompt assets; callers must include the explicit version in cache identity."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    """An immutable prompt identity and bounded system instruction."""

    version: str
    system_instruction: str
