"""FFmpeg-python based atomic MP3 merger for validated private TTS segments."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import ffmpeg

from dailycast.core.hashes import sha256_bytes
from dailycast.tts.contracts import MergedAudio


class AudioMergeError(RuntimeError):
    """Raised when FFmpeg cannot turn all succeeded segments into one playable draft."""


class FFmpegMerger:
    """Decode/re-encode ordered MP3 inputs, then atomically promote without shell concatenation."""

    def __init__(self, *, sample_rate: int, bitrate: str) -> None:
        self._sample_rate = sample_rate
        self._bitrate = bitrate

    def merge(self, input_paths: tuple[Path, ...], output_path: Path) -> MergedAudio:
        """Use ffmpeg-python to create, validate, and atomically replace one MP3 file."""
        if not input_paths or any(not path.is_file() for path in input_paths):
            raise AudioMergeError("all succeeded segment files must exist before merge")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = output_path.with_name(f".{output_path.name}.part")
        try:
            self._render(input_paths, temporary)
            if not temporary.is_file() or temporary.stat().st_size == 0:
                raise AudioMergeError("FFmpeg produced an empty draft audio file")
            duration_ms, sample_rate = self._probe(temporary)
            payload = temporary.read_bytes()
            os.replace(temporary, output_path)
            return MergedAudio(
                duration_ms=duration_ms,
                sample_rate=sample_rate,
                byte_size=len(payload),
                sha256=sha256_bytes(payload),
            )
        except ffmpeg.Error as error:
            if temporary.exists():
                temporary.unlink()
            details = error.stderr.decode("utf-8", errors="replace")[:1000]
            raise AudioMergeError(f"FFmpeg merge failed: {details}") from error
        except Exception:
            if temporary.exists():
                temporary.unlink()
            raise

    def _render(self, input_paths: tuple[Path, ...], temporary: Path) -> None:
        """Construct a filter graph through ffmpeg-python without a shell command string."""
        inputs = tuple(ffmpeg.input(str(path)).audio for path in input_paths)
        audio = inputs[0] if len(inputs) == 1 else ffmpeg.concat(*inputs, v=0, a=1)
        (
            ffmpeg.output(
                audio,
                str(temporary),
                acodec="libmp3lame",
                ar=self._sample_rate,
                audio_bitrate=self._bitrate,
                format="mp3",
            )
            .overwrite_output()
            .run(capture_stdout=True, capture_stderr=True)
        )

    @staticmethod
    def _probe(path: Path) -> tuple[int, int]:
        """Read duration and sample rate from the final temporary file before it becomes visible."""
        metadata: dict[str, Any] = ffmpeg.probe(str(path))
        streams = metadata.get("streams", [])
        audio_stream = next(
            (stream for stream in streams if stream.get("codec_type") == "audio"), None
        )
        if not isinstance(audio_stream, dict):
            raise AudioMergeError("FFmpeg output has no decodable audio stream")
        raw_duration = audio_stream.get("duration") or metadata.get("format", {}).get("duration")
        raw_sample_rate = audio_stream.get("sample_rate")
        if not isinstance(raw_sample_rate, (str, int)):
            raise AudioMergeError("FFmpeg output metadata is incomplete")
        try:
            duration_ms = round(float(raw_duration) * 1000)
            sample_rate = int(raw_sample_rate)
        except (TypeError, ValueError) as error:
            raise AudioMergeError("FFmpeg output metadata is incomplete") from error
        if duration_ms <= 0 or sample_rate <= 0:
            raise AudioMergeError("FFmpeg output duration or sample rate is invalid")
        return duration_ms, sample_rate
