"""Versioned prompt identity for bounded plain-text podcast metadata generation."""

from dailycast.llm.prompts import PromptTemplate

GENERATE_METADATA_V1 = PromptTemplate(
    version="generate_metadata_v1",
    system_instruction=(
        "Return only strict JSON. Create concise plain-text Chinese podcast metadata from supplied "
        "event titles, bounded validated script text, and estimated duration. "
        "Do not add unsupported "
        "facts, Markdown, citation syntax, URLs, secrets, or source content."
    ),
)
