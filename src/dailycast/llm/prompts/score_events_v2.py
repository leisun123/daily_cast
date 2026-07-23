"""Explicit JSON-object contract for Responses API event scoring."""

from dailycast.llm.prompts import PromptTemplate

SCORE_EVENTS_V2 = PromptTemplate(
    version="score_events_v2",
    system_instruction=(
        "Evaluate only the supplied news-event evidence; do not invent facts or sources. "
        "Return exactly one JSON object, with no Markdown and no keys other than `scores`. "
        "Its required shape is: "
        '{"scores":[{"event_id":<integer>,"importance":<number 0-100>,'
        '"relevance":<number 0-100>,"confidence":<number 0-100>,'
        '"recommend":<boolean>,"reason":<non-empty string up to 600 characters>,'
        '"risks":<array of at most 8 strings>}]} . '
        "Return exactly one score object for every supplied event_id, each supplied event_id "
        "exactly once, and never add an unknown event_id. `recommend` must be the JSON "
        "boolean true or false, not a string."
    ),
)
