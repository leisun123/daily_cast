"""Explicit prompt identity for evidence-constrained episode-outline generation."""

from dailycast.llm.prompts import PromptTemplate

GENERATE_OUTLINE_V1 = PromptTemplate(
    version="generate_outline_v1",
    system_instruction=(
        "Return only the requested JSON object. Create an outline using only the supplied "
        "selected-event EvidenceDossiers. Do not invent facts, sources, articles, or event IDs. "
        "Give higher-ranked events more time, keep intro and outro short, and obey the exact "
        "section, event-ID, duration, and schema constraints in the user payload."
    ),
)
