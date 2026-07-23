"""Versioned prompt identity for bounded evidence-only script review."""

from dailycast.llm.prompts import PromptTemplate

REVIEW_SCRIPT_V1 = PromptTemplate(
    version="review_script_v1",
    system_instruction=(
        "Return only strict JSON. Review the supplied script only against the provided bounded "
        "EvidenceDossiers. Do not claim independent internet fact-checking. Identify unsupported "
        "claims, contradictions, repetition, and poor spoken style without inventing replacement "
        "facts. Reference only supplied section IDs and article IDs."
    ),
)
