"""Stable section-level segmentation for V1 podcast scripts."""

from __future__ import annotations

from dataclasses import dataclass

from dailycast.core.hashes import sha256_text
from dailycast.llm.script_schemas import EpisodeScript

SECTION_SEGMENTER_VERSION = "section-v1"


@dataclass(frozen=True, slots=True)
class ScriptSegment:
    """One ordered, indivisible EpisodeScript section prepared for TTS."""

    segment_index: int
    section_id: str
    script_revision: int
    text: str
    text_hash: str


def segment_episode_script(script: object, *, script_revision: int) -> tuple[ScriptSegment, ...]:
    """Map each validated script section to exactly one stable synthesis segment."""
    validated_script = EpisodeScript.model_validate(script)
    if script_revision < 1:
        raise ValueError("script_revision must be positive before TTS segmentation")
    return tuple(
        ScriptSegment(
            segment_index=index,
            section_id=section.section_id,
            script_revision=script_revision,
            text=section.text,
            text_hash=sha256_text(section.text),
        )
        for index, section in enumerate(validated_script.sections)
    )
