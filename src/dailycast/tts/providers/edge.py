"""Async Edge TTS provider isolated behind the DailyCast TTS contract."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import edge_tts

from dailycast.core.hashes import sha256_text
from dailycast.tts.contracts import AudioResult, TextMode


class EdgeTTSProvider:
    """Use edge-tts asynchronously with timeout, bounded retry, and cancellation propagation."""

    provider_name = "edge_tts"
    model = "edge-tts"

    def __init__(
        self,
        *,
        timeout_seconds: float,
        max_retries: int,
        communicate_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._communicate_factory = communicate_factory or edge_tts.Communicate

    def provider_config_hash(self) -> str:
        """Return a non-secret identity for this edge-tts implementation and output mode."""
        return sha256_text(
            json.dumps(
                {
                    "implementation": "edge-tts-python-v7-enhanced-text-mp3",
                    "endpoint_identity": "edge-tts-service",
                    "semantic_options": {"output_format": "edge-tts-default-mp3"},
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )

    async def synthesize(
        self,
        text: str,
        voice: str,
        speed: float,
        format: str,
        *,
        text_mode: TextMode = "plain",
    ) -> AudioResult:
        """Stream audio bytes with bounded retry while propagating cancellation."""
        if format != "mp3":
            raise ValueError("EdgeTTSProvider supports only mp3")
        if text_mode not in {"plain", "enhanced_text"}:
            raise ValueError("EdgeTTSProvider text_mode must be plain or enhanced_text")
        for attempt in range(self._max_retries + 1):
            try:
                return await asyncio.wait_for(
                    self._synthesize_once(text, voice, speed, text_mode),
                    timeout=self._timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                if attempt >= self._max_retries:
                    raise
                await asyncio.sleep(0.1 * (2**attempt))
        raise RuntimeError("unreachable Edge TTS retry state")

    async def _synthesize_once(
        self, text: str, voice: str, speed: float, text_mode: TextMode
    ) -> AudioResult:
        """Collect the provider audio stream without executing a shell command or creating files."""
        communicate_factory: Any = self._communicate_factory
        communication = communicate_factory(
            text,
            voice=voice,
            rate=_edge_rate(speed),
        )
        chunks: list[bytes] = []
        async for chunk in communication.stream():
            if chunk.get("type") == "audio":
                data = chunk.get("data")
                if isinstance(data, bytes):
                    chunks.append(data)
        audio_bytes = b"".join(chunks)
        if not audio_bytes:
            raise RuntimeError("Edge TTS returned no audio bytes")
        return AudioResult(
            audio_bytes=audio_bytes,
            duration_ms=None,
            sample_rate=24_000,
            mime_type="audio/mpeg",
            provider_request_id=None,
        )


def _edge_rate(speed: float) -> str:
    """Convert the provider-neutral speed multiplier to Edge's bounded percentage syntax."""
    percentage = round((speed - 1.0) * 100)
    return f"{percentage:+d}%"
