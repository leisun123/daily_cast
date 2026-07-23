"""Versioned prompt asset for the future event-scoring workflow."""

from dailycast.llm.prompts import PromptTemplate

SCORE_EVENTS_V1 = PromptTemplate(
    version="score_events_v1",
    system_instruction=(
        "Return only the requested JSON object. Evaluate supplied news-event evidence "
        "without inventing facts or sources."
    ),
)
