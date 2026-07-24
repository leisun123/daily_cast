"""Episode draft-audio generation with durable segment checkpoints and semantic cache reuse."""

from __future__ import annotations

import asyncio
import json
import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from dailycast.core.errors import DailyCastError
from dailycast.core.hashes import sha256_bytes, sha256_text
from dailycast.db.models import AudioSegment, AudioSegmentStatus, Episode, EpisodeStatus
from dailycast.db.repositories import AudioSegmentRepository, EpisodeRepository
from dailycast.db.transactions import UnitOfWork
from dailycast.llm.script_schemas import EpisodeScript
from dailycast.tts.contracts import AudioMerger, AudioResult, TextMode, TTSProvider
from dailycast.tts.preprocess import PreparedSpeech, SectionRole, TTSPreprocessor
from dailycast.tts.segmenter import SECTION_SEGMENTER_VERSION, ScriptSegment, segment_episode_script


class AudioGenerationError(DailyCastError):
    """Raised after durable segment state records make an audio generation failure resumable."""

    def __init__(
        self,
        message: str = "Episode draft audio generation failed",
        *,
        retryable: bool = True,
    ) -> None:
        super().__init__(
            code="AUDIO_GENERATION_FAILED",
            message=message,
            status_code=502,
            retryable=retryable,
        )


@dataclass(frozen=True, slots=True)
class TTSGenerationSettings:
    """Semantic TTS choices, excluding timeout and retry transport policy."""

    voice: str
    speed: float = 1.0
    format: str = "mp3"
    text_mode: TextMode = "plain"
    opening_summary_speed: float = 0.94
    cache_enabled: bool = True
    segmenter_version: str = SECTION_SEGMENTER_VERSION


@dataclass(frozen=True, slots=True)
class AudioGenerationResult:
    """Durable work metrics reported by the generate_audio TaskStep."""

    episode_id: int
    segment_count: int
    cache_hits: int
    provider_calls: int
    tts_character_count: int
    duration_ms: int
    audio_version: int
    draft_audio_path: str


@dataclass(frozen=True, slots=True)
class _SegmentOutcome:
    """Internal per-segment result used to aggregate TaskStep metrics."""

    segment: AudioSegment
    cache_hit: bool
    provider_call: bool
    tts_character_count: int


class AudioGenerationService:
    """Own TTS cache identity, retries, filesystem promotion, merge, and Episode audio state."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        provider: TTSProvider,
        *,
        data_dir: Path,
        merger: AudioMerger,
        settings: TTSGenerationSettings,
        preprocessor: TTSPreprocessor | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._provider = provider
        self._data_dir = data_dir.resolve()
        self._merger = merger
        self._settings = settings
        self._preprocessor = preprocessor or TTSPreprocessor(text_mode=settings.text_mode)

    async def generate_episode_draft(self, episode_id: int) -> AudioGenerationResult:
        """Create/resume revision segments and atomically promote the merged public draft."""
        episode, script_segments, pronunciation_hints = self._load_episode_segments(episode_id)
        self._invalidate_approval_for_audio_change(episode_id)
        outcomes: list[_SegmentOutcome] = []
        for index, script_segment in enumerate(script_segments):
            outcomes.append(
                await self._ensure_segment(
                    episode_id,
                    script_segment,
                    section_role=_section_role(index, len(script_segments)),
                    pronunciation_hints=pronunciation_hints,
                )
            )

        durable_segments = self._load_ready_segments(episode_id, episode.script_revision)
        if len(durable_segments) != len(script_segments) or any(
            segment.status is not AudioSegmentStatus.SUCCEEDED for segment in durable_segments
        ):
            raise AudioGenerationError("Episode has missing or failed TTS segments")
        return await self._merge_and_finalize(
            episode_id=episode_id,
            script_revision=episode.script_revision,
            segments=durable_segments,
            cache_hits=sum(outcome.cache_hit for outcome in outcomes),
            provider_calls=sum(outcome.provider_call for outcome in outcomes),
            tts_character_count=sum(
                outcome.tts_character_count for outcome in outcomes if outcome.provider_call
            ),
        )

    def _load_episode_segments(
        self, episode_id: int
    ) -> tuple[Episode, tuple[ScriptSegment, ...], tuple[tuple[str, str], ...]]:
        """Validate the persisted structured script before doing any provider I/O."""
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            episode = EpisodeRepository(unit.session).get(episode_id)
            if episode is None:
                raise AudioGenerationError(f"Episode {episode_id} does not exist", retryable=False)
            if episode.status in {EpisodeStatus.PUBLISHED, EpisodeStatus.PUBLISHING}:
                raise AudioGenerationError(
                    "Published or publishing Episode draft audio cannot be changed", retryable=False
                )
            if episode.script_json is None or episode.script_revision < 1:
                raise AudioGenerationError(
                    "Episode has no valid persisted script revision", retryable=False
                )
            try:
                script = json.loads(episode.script_json)
                script_segments = segment_episode_script(
                    script, script_revision=episode.script_revision
                )
                hints = tuple(
                    (hint.term, hint.pronunciation)
                    for hint in EpisodeScript.model_validate(script).pronunciation_hints
                )
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                raise AudioGenerationError(
                    "Episode script is not valid for TTS segmentation", retryable=False
                ) from error
            if not script_segments:
                raise AudioGenerationError(
                    "Episode script contains no synthesizable sections", retryable=False
                )
            return episode, script_segments, hints

    def _invalidate_approval_for_audio_change(self, episode_id: int) -> None:
        """Move an approved/reviewable draft to draft while audio validity is being rebuilt."""
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            episode = EpisodeRepository(unit.session).get(episode_id)
            if episode is None:
                raise AudioGenerationError(f"Episode {episode_id} does not exist")
            if episode.status is EpisodeStatus.APPROVED:
                episode.approved_script_revision = None
                episode.approved_audio_version = None
                episode.approved_at = None
            if episode.status in {EpisodeStatus.APPROVED, EpisodeStatus.REVIEW_REQUIRED}:
                episode.status = EpisodeStatus.DRAFT
            episode.updated_at = episode.updated_at
            unit.session.flush()

    async def _ensure_segment(
        self,
        episode_id: int,
        script_segment: ScriptSegment,
        *,
        section_role: SectionRole,
        pronunciation_hints: tuple[tuple[str, str], ...],
    ) -> _SegmentOutcome:
        """Reuse a validated cache file or durably synthesize exactly one needed segment."""
        prepared = self._preprocessor.prepare(
            script_segment.text,
            section_role=section_role,
            pronunciation_hints=pronunciation_hints,
        )
        speed = _segment_speed(self._settings, section_role)
        provider_config_hash = _prepared_provider_config_hash(
            self._provider.provider_config_hash(), prepared.semantic_hash
        )
        cache_key = _audio_cache_key(
            provider=self._provider.provider_name,
            provider_config_hash=provider_config_hash,
            model=self._provider.model,
            voice=self._settings.voice,
            speed=speed,
            format=self._settings.format,
            segmenter_version=self._settings.segmenter_version,
            text=prepared.text,
        )
        current = self._prepare_current_segment(
            episode_id=episode_id,
            script_segment=script_segment,
            prepared=prepared,
            speed=speed,
            provider_config_hash=provider_config_hash,
            cache_key=cache_key,
        )
        if self._is_valid_succeeded_segment(current):
            return _SegmentOutcome(
                segment=current, cache_hit=False, provider_call=False, tts_character_count=0
            )
        if self._settings.cache_enabled:
            cached = self._lookup_valid_cache(cache_key, provider_config_hash, current.id)
            if cached is not None:
                return _SegmentOutcome(
                    segment=cached, cache_hit=True, provider_call=False, tts_character_count=0
                )
        self._mark_synthesizing(current.id)
        try:
            result = await self._provider.synthesize(
                prepared.text,
                self._settings.voice,
                speed,
                self._settings.format,
                text_mode=prepared.text_mode,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._mark_failed(current.id, error)
            raise AudioGenerationError(
                "TTS provider could not synthesize an audio segment"
            ) from error
        return _SegmentOutcome(
            segment=self._persist_succeeded_result(current.id, result),
            cache_hit=False,
            provider_call=True,
            tts_character_count=prepared.spoken_character_count,
        )

    def _prepare_current_segment(
        self,
        *,
        episode_id: int,
        script_segment: ScriptSegment,
        prepared: PreparedSpeech,
        speed: float,
        provider_config_hash: str,
        cache_key: str,
    ) -> AudioSegment:
        """Create/reset the durable row for a revision position before cache/provider work."""
        values = {
            "segmenter_version": self._settings.segmenter_version,
            "text": prepared.text,
            "text_hash": sha256_text(prepared.text),
            "cache_key": cache_key,
            "provider": self._provider.provider_name,
            "model": self._provider.model,
            "voice": self._settings.voice,
            "speed": speed,
            "format": self._settings.format,
            "provider_config_hash": provider_config_hash,
        }
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            repository = AudioSegmentRepository(unit.session)
            current = repository.get_by_episode_revision_index(
                episode_id, script_segment.script_revision, script_segment.segment_index
            )
            if current is None:
                return repository.create(
                    episode_id=episode_id,
                    script_revision=script_segment.script_revision,
                    segment_index=script_segment.segment_index,
                    status=AudioSegmentStatus.PENDING,
                    **values,
                )
            if self._matches_semantics(current, values) and self._is_valid_succeeded_segment(
                current
            ):
                return current
            return repository.update(
                current,
                status=AudioSegmentStatus.PENDING,
                audio_path=None,
                mime_type=None,
                byte_size=None,
                sha256=None,
                duration_ms=None,
                provider_request_id=None,
                error_code=None,
                error_summary=None,
                force_nonce=None,
                **values,
            )

    def _lookup_valid_cache(
        self, cache_key: str, provider_config_hash: str, current_id: int
    ) -> AudioSegment | None:
        """Promote a verified cache file into the current segment without another provider call."""
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            repository = AudioSegmentRepository(unit.session)
            cached = repository.get_by_cache_key(cache_key, provider_config_hash)
            current = unit.session.get(AudioSegment, current_id)
            if cached is None or current is None or cached.id == current.id:
                return None
            if not self._is_valid_succeeded_segment(cached):
                return None
            return repository.update(
                current,
                status=AudioSegmentStatus.SUCCEEDED,
                audio_path=cached.audio_path,
                mime_type=cached.mime_type,
                byte_size=cached.byte_size,
                sha256=cached.sha256,
                duration_ms=cached.duration_ms,
                provider_request_id=cached.provider_request_id,
                error_code=None,
                error_summary=None,
            )

    def _mark_synthesizing(self, segment_id: int) -> None:
        """Record in-progress external work before any provider request is made."""
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            segment = unit.session.get(AudioSegment, segment_id)
            if segment is None:
                raise AudioGenerationError("Audio segment disappeared before synthesis")
            AudioSegmentRepository(unit.session).update(
                segment,
                status=AudioSegmentStatus.SYNTHESIZING,
                attempt_count=segment.attempt_count + 1,
                error_code=None,
                error_summary=None,
            )

    def _mark_failed(self, segment_id: int, error: Exception) -> None:
        """Keep a short safe error record so the next run retries only this segment."""
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            segment = unit.session.get(AudioSegment, segment_id)
            if segment is not None:
                AudioSegmentRepository(unit.session).update(
                    segment,
                    status=AudioSegmentStatus.FAILED,
                    error_code="TTS_PROVIDER_FAILED",
                    error_summary=(str(error) or error.__class__.__name__)[:1000],
                )

    def _persist_succeeded_result(self, segment_id: int, result: AudioResult) -> AudioSegment:
        """Atomically write private cached bytes, checksum them, then mark the segment succeeded."""
        if not result.audio_bytes:
            self._mark_failed(segment_id, ValueError("provider returned empty audio"))
            raise AudioGenerationError("TTS provider returned empty audio")
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            segment = unit.session.get(AudioSegment, segment_id)
            if segment is None:
                raise AudioGenerationError("Audio segment disappeared after synthesis")
            relative_path = self._cache_relative_path(segment)
            output_path = self._safe_data_path(relative_path)
            _atomic_write_bytes(output_path, result.audio_bytes)
            return AudioSegmentRepository(unit.session).update(
                segment,
                status=AudioSegmentStatus.SUCCEEDED,
                audio_path=relative_path.as_posix(),
                mime_type=result.mime_type,
                byte_size=len(result.audio_bytes),
                sha256=sha256_bytes(result.audio_bytes),
                duration_ms=result.duration_ms or _fallback_duration_ms(segment.text),
                provider_request_id=result.provider_request_id,
                error_code=None,
                error_summary=None,
            )

    def _load_ready_segments(self, episode_id: int, script_revision: int) -> list[AudioSegment]:
        """Load the complete revision from SQLite after each short synthesis transaction commits."""
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            return AudioSegmentRepository(unit.session).list_by_episode_revision(
                episode_id, script_revision=script_revision
            )

    async def _merge_and_finalize(
        self,
        *,
        episode_id: int,
        script_revision: int,
        segments: list[AudioSegment],
        cache_hits: int,
        provider_calls: int,
        tts_character_count: int,
    ) -> AudioGenerationResult:
        """Merge ready private files into a private mutable draft before publication promotion."""
        manifest_hash = _manifest_hash(segments)
        draft_relative = (
            Path("audio") / "drafts" / str(episode_id) / f"revision-{script_revision}.mp3"
        )
        draft_output = self._safe_data_path(draft_relative)
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            episode = EpisodeRepository(unit.session).get(episode_id)
            if episode is None:
                raise AudioGenerationError(f"Episode {episode_id} does not exist")
            if (
                episode.script_revision == script_revision
                and episode.audio_manifest_hash == manifest_hash
                and episode.draft_audio_path == draft_relative.as_posix()
                and episode.draft_audio_sha256 is not None
                and self._is_valid_data_audio(draft_relative, episode.draft_audio_sha256)
            ):
                episode.status = EpisodeStatus.REVIEW_REQUIRED
                unit.session.flush()
                return AudioGenerationResult(
                    episode_id=episode_id,
                    segment_count=len(segments),
                    cache_hits=cache_hits,
                    provider_calls=provider_calls,
                    duration_ms=episode.actual_duration_ms or 0,
                    audio_version=episode.audio_version,
                    draft_audio_path=draft_relative.as_posix(),
                    tts_character_count=tts_character_count,
                )
        input_paths = tuple(
            self._safe_data_path(Path(segment.audio_path or "")) for segment in segments
        )
        try:
            merged = await asyncio.to_thread(self._merger.merge, input_paths, draft_output)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise AudioGenerationError("FFmpeg could not merge Episode draft audio") from error
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            episode = EpisodeRepository(unit.session).get(episode_id)
            if episode is None:
                raise AudioGenerationError(f"Episode {episode_id} does not exist")
            if episode.script_revision != script_revision:
                raise AudioGenerationError("Episode script changed while audio was being generated")
            episode.audio_version += 1
            episode.audio_manifest_hash = manifest_hash
            episode.draft_audio_path = draft_relative.as_posix()
            episode.draft_audio_sha256 = merged.sha256
            episode.actual_duration_ms = merged.duration_ms
            episode.error_code = None
            episode.error_summary = None
            episode.status = EpisodeStatus.REVIEW_REQUIRED
            episode.updated_at = episode.updated_at
            unit.session.flush()
            return AudioGenerationResult(
                episode_id=episode_id,
                segment_count=len(segments),
                cache_hits=cache_hits,
                provider_calls=provider_calls,
                duration_ms=merged.duration_ms,
                audio_version=episode.audio_version,
                draft_audio_path=draft_relative.as_posix(),
                tts_character_count=tts_character_count,
            )

    def _is_valid_succeeded_segment(self, segment: AudioSegment) -> bool:
        """Use only a succeeded row whose private cached file still matches its stored checksum."""
        return (
            segment.status is AudioSegmentStatus.SUCCEEDED
            and segment.audio_path is not None
            and segment.sha256 is not None
            and self._is_valid_data_audio(Path(segment.audio_path), segment.sha256)
        )

    def _is_valid_data_audio(self, relative_path: Path, expected_hash: str) -> bool:
        """Reject escaped, missing, or checksum-mismatched cache paths before reuse or merge."""
        try:
            path = self._safe_data_path(relative_path)
        except AudioGenerationError:
            return False
        return path.is_file() and sha256_bytes(path.read_bytes()) == expected_hash

    def _safe_data_path(self, relative_path: Path) -> Path:
        """Resolve only a relative cache path below configured DATA_DIR."""
        return _safe_child_path(self._data_dir, relative_path)

    @staticmethod
    def _matches_semantics(segment: AudioSegment, values: dict[str, object]) -> bool:
        """Require exact matching identity before a row's ready audio can be retained."""
        return all(getattr(segment, field) == value for field, value in values.items())

    @staticmethod
    def _cache_relative_path(segment: AudioSegment) -> Path:
        """Use content-addressed private cache files for safe cross-episode reuse."""
        return Path("audio") / "cache" / segment.cache_key[:2] / f"{segment.cache_key}.mp3"


def _audio_cache_key(
    *,
    provider: str,
    provider_config_hash: str,
    model: str,
    voice: str,
    speed: float,
    format: str,
    segmenter_version: str,
    text: str,
) -> str:
    """Hash semantic audio inputs; secrets, timeouts, and retry policy do not enter."""
    canonical = json.dumps(
        {
            "format": format,
            "model": model,
            "normalized_text": _normalized_text(text),
            "provider": provider,
            "provider_config_hash": provider_config_hash,
            "segmenter_version": segmenter_version,
            "speed": f"{speed:.6f}",
            "voice": voice,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return sha256_text(canonical)


def _normalized_text(text: str) -> str:
    """Normalize Unicode representation and outer whitespace without changing spoken content."""
    return unicodedata.normalize("NFKC", text).strip()


def _prepared_provider_config_hash(provider_hash: str, preprocess_hash: str) -> str:
    """Bind the provider cache identity to the non-secret speech-preparation policy."""
    return sha256_text(
        json.dumps(
            {"preprocess_hash": preprocess_hash, "provider_hash": provider_hash},
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def _section_role(index: int, total: int) -> SectionRole:
    """Use gentler delivery for the opening and summary without changing stored script text."""
    if index == 0:
        return "opening"
    if index == total - 1:
        return "closing"
    return "body"


def _segment_speed(settings: TTSGenerationSettings, section_role: SectionRole) -> float:
    """Keep news body cadence normal while slowing the opening and final summary slightly."""
    if section_role in {"opening", "closing"}:
        return settings.opening_summary_speed
    return settings.speed


def _fallback_duration_ms(text: str) -> int:
    """Keep a provisional duration; FFmpeg later measures the merged output."""
    return max(1, len(_normalized_text(text)) * 60)


def _manifest_hash(segments: list[AudioSegment]) -> str:
    """Fingerprint the ordered merged source files so identical retry work is a no-op."""
    return sha256_text(
        json.dumps(
            [
                {
                    "index": segment.segment_index,
                    "path": segment.audio_path,
                    "sha256": segment.sha256,
                }
                for segment in segments
            ],
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def _safe_child_path(root: Path, relative_path: Path) -> Path:
    """Return a path under a configured root, rejecting traversal and absolute inputs."""
    if not relative_path.parts or relative_path.is_absolute():
        raise AudioGenerationError("audio path must be relative to configured storage")
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise AudioGenerationError("audio path escapes configured storage") from error
    return candidate


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Write via a sibling temporary file so interrupted cache writes are never hits."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.part")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
