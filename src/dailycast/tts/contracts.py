"""Provider-neutral contracts for deterministic text-to-speech synthesis."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class AudioResult:
    """One provider response kept in memory until the service atomically persists it."""

    audio_bytes: bytes
    duration_ms: int | None
    sample_rate: int | None
    mime_type: str
    provider_request_id: str | None
    cache_key: str | None = None


@dataclass(frozen=True, slots=True)
class MergedAudio:
    """Verified metadata for an atomically promoted draft MP3."""

    duration_ms: int
    sample_rate: int
    byte_size: int
    sha256: str


class TTSProvider(Protocol):
    """Isolate provider APIs from segmentation, filesystem, cache, and Episode state decisions."""

    provider_name: str
    model: str

    def provider_config_hash(self) -> str:
        """Return the non-secret semantic identity used with the complete cache key."""

    async def synthesize(self, text: str, voice: str, speed: float, format: str) -> AudioResult:
        """Return synthesized audio bytes without writing a cache or changing database state."""


class AudioMerger(Protocol):
    """Merge ordered private segments into one atomically promoted draft audio file."""

    def merge(self, input_paths: tuple[Path, ...], output_path: Path) -> MergedAudio:
        """Create and atomically replace the final output path after FFmpeg validation."""
