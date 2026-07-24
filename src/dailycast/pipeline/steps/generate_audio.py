"""Pipeline checkpoint that creates resumable TTS segments and one Episode draft audio file."""

from __future__ import annotations

import json
from dataclasses import dataclass

from dailycast.pipeline.context import PipelineContext
from dailycast.pipeline.contracts import JSONValue, StepResult
from dailycast.tts.service import AudioGenerationService


@dataclass(frozen=True, slots=True)
class GenerateAudioStep:
    """Generate draft audio after CreateEpisodeStep persists a durable Episode identity."""

    audio_service: AudioGenerationService
    name: str = "generate_audio"

    async def run(self, context: PipelineContext) -> StepResult:
        """Generate/reuse segments and return compact metrics for the owning TaskStep."""
        _active_task_step_id(context)
        episode_id = context.values.get("episode_id")
        if not isinstance(episode_id, int):
            raise RuntimeError("generate_audio requires an Episode produced by create_episode")
        result = await self.audio_service.generate_episode_draft(episode_id)
        details: dict[str, JSONValue] = {
            "audio_version": result.audio_version,
            "cache_hits": result.cache_hits,
            "draft_audio_path": result.draft_audio_path,
            "duration_ms": result.duration_ms,
            "episode_id": result.episode_id,
            "provider_calls": result.provider_calls,
            "segment_count": result.segment_count,
            "tts_character_count": result.tts_character_count,
        }
        return StepResult(
            input_count=result.segment_count,
            output_count=1,
            checkpoint_json=json.dumps(details, separators=(",", ":"), sort_keys=True),
            details=details,
            artifact_path=result.draft_audio_path,
            tts_character_count=result.tts_character_count,
        )

    def restore_checkpoint(self, context: PipelineContext, checkpoint: dict[str, object]) -> None:
        """Keep the Episode identity available when a successful audio checkpoint is inherited."""
        episode_id = checkpoint.get("episode_id")
        if isinstance(episode_id, int) and episode_id > 0:
            context.values["episode_id"] = episode_id


def _active_task_step_id(context: PipelineContext) -> int:
    """Require the durable TaskStep created by the sequential pipeline orchestrator."""
    task_step_id = context.values.get("active_task_step_id")
    if not isinstance(task_step_id, int):
        raise RuntimeError("generate_audio requires an active persisted TaskStep ID")
    return task_step_id
