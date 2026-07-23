"""Sprint 5B deterministic audio generation, recovery, and pipeline coverage."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker
from test_episode_service import accepted_artifacts, create_episode

from dailycast.core.hashes import sha256_bytes
from dailycast.core.time import Clock
from dailycast.db.models import AudioSegmentStatus
from dailycast.db.repositories import AudioSegmentRepository, TaskRunRepository, TaskStepRepository
from dailycast.db.transactions import UnitOfWork
from dailycast.episodes.service import EpisodeService
from dailycast.pipeline.context import PipelineContext
from dailycast.pipeline.steps.generate_audio import GenerateAudioStep
from dailycast.tts.contracts import MergedAudio
from dailycast.tts.providers.edge import EdgeTTSProvider
from dailycast.tts.providers.fake import FakeTTSProvider
from dailycast.tts.segmenter import segment_episode_script
from dailycast.tts.service import (
    AudioGenerationError,
    AudioGenerationService,
    TTSGenerationSettings,
)


class AtomicFakeMerger:
    """Test merger that verifies the service passes ordered files and promotes atomically."""

    def __init__(self) -> None:
        self.calls: list[tuple[Path, ...]] = []

    def merge(self, input_paths: tuple[Path, ...], output_path: Path) -> MergedAudio:
        """Persist deterministic bytes through a temporary sibling path for atomic-output tests."""
        self.calls.append(input_paths)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = output_path.with_name(f".{output_path.name}.part")
        payload = b"merged:" + b"|".join(path.read_bytes() for path in input_paths)
        temporary.write_bytes(payload)
        os.replace(temporary, output_path)
        return MergedAudio(
            duration_ms=len(input_paths) * 1000,
            sample_rate=24_000,
            byte_size=len(payload),
            sha256=sha256_bytes(payload),
        )


def audio_service(
    factory: sessionmaker[Session],
    provider: FakeTTSProvider,
    tmp_path: Path,
    *,
    voice: str = "zh-CN-XiaoxiaoNeural",
) -> tuple[AudioGenerationService, AtomicFakeMerger]:
    """Build a local service whose output roots are supplied only by configuration."""
    merger = AtomicFakeMerger()
    return (
        AudioGenerationService(
            factory,
            provider,
            data_dir=tmp_path / "data",
            public_dir=tmp_path / "public",
            merger=merger,
            settings=TTSGenerationSettings(voice=voice, speed=1.0),
        ),
        merger,
    )


def episode_for_audio(factory: sessionmaker[Session], *, key: str, day: int) -> object:
    """Create a persisted, validated Episode whose stable script has one segment per section."""
    artifacts = replace(accepted_artifacts(factory, key=key), episode_date=replace_date(day))
    return create_episode(EpisodeService(factory), artifacts)


def replace_date(day: int):
    """Return a distinct July business day without changing the shared editorial test fixture."""
    from datetime import date

    return date(2026, 7, day)


def test_segmenter_preserves_one_segment_per_script_section(app_config_path: Path) -> None:
    """Stable segmentation never splits an already validated EpisodeScript section."""
    factory: sessionmaker[Session] = __import__(
        "editorial_test_support", fromlist=["upgraded_session_factory"]
    ).upgraded_session_factory(app_config_path)
    try:
        artifacts = accepted_artifacts(factory, key="segments")

        segments = segment_episode_script(artifacts.script, script_revision=1)

        assert [segment.segment_index for segment in segments] == [0, 1, 2]
        assert [segment.section_id for segment in segments] == ["intro", "news-1", "outro"]
        assert [segment.text for segment in segments] == [
            section.text for section in artifacts.script.sections
        ]
    finally:
        factory.kw["bind"].dispose()


def test_audio_generation_creates_succeeded_segments_merges_and_updates_episode(
    app_config_path: Path, tmp_path: Path
) -> None:
    """A synthesis run stores segments, atomically creates draft audio, and updates Episode."""
    factory: sessionmaker[Session] = __import__(
        "editorial_test_support", fromlist=["upgraded_session_factory"]
    ).upgraded_session_factory(app_config_path)
    try:
        episode = episode_for_audio(factory, key="audio-create", day=22)
        provider = FakeTTSProvider()
        service, merger = audio_service(factory, provider, tmp_path)

        result = asyncio.run(service.generate_episode_draft(episode.id))

        assert result.segment_count == 3
        assert result.provider_calls == 3
        assert result.cache_hits == 0
        assert result.duration_ms == 3000
        assert len(merger.calls) == 1
        output = tmp_path / "public" / "audio" / f"{episode.id}.mp3"
        assert output.is_file()
        assert not output.with_name(f".{output.name}.part").exists()
        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            segments = AudioSegmentRepository(unit.session).list_by_episode_revision(
                episode.id, script_revision=1
            )
            assert [segment.status for segment in segments] == [AudioSegmentStatus.SUCCEEDED] * 3
            assert all(segment.audio_path is not None for segment in segments)
            persisted = EpisodeService(factory).get_episode(episode.id)
            assert persisted is not None
            assert persisted.audio_version == 1
            assert persisted.actual_duration_ms == 3000
            assert persisted.draft_audio_path == f"audio/{episode.id}.mp3"
    finally:
        factory.kw["bind"].dispose()


def test_audio_cache_reuses_matching_semantic_segment_files(
    app_config_path: Path, tmp_path: Path
) -> None:
    """Matching provider and text identities reuse succeeded cache files without provider calls."""
    factory: sessionmaker[Session] = __import__(
        "editorial_test_support", fromlist=["upgraded_session_factory"]
    ).upgraded_session_factory(app_config_path)
    try:
        provider = FakeTTSProvider()
        service, _ = audio_service(factory, provider, tmp_path)
        first = episode_for_audio(factory, key="cache-first", day=22)
        second = episode_for_audio(factory, key="cache-second", day=23)

        asyncio.run(service.generate_episode_draft(first.id))
        reused = asyncio.run(service.generate_episode_draft(second.id))

        assert provider.calls == 3
        assert reused.cache_hits == 3
        assert reused.provider_calls == 0
    finally:
        factory.kw["bind"].dispose()


def test_changed_voice_is_a_cache_miss(app_config_path: Path, tmp_path: Path) -> None:
    """Voice is part of the semantic cache identity and cannot reuse another voice's audio."""
    factory: sessionmaker[Session] = __import__(
        "editorial_test_support", fromlist=["upgraded_session_factory"]
    ).upgraded_session_factory(app_config_path)
    try:
        provider = FakeTTSProvider()
        first_service, _ = audio_service(factory, provider, tmp_path)
        second_service, _ = audio_service(factory, provider, tmp_path, voice="zh-CN-YunxiNeural")
        first = episode_for_audio(factory, key="voice-first", day=22)
        second = episode_for_audio(factory, key="voice-second", day=23)

        asyncio.run(first_service.generate_episode_draft(first.id))
        missed = asyncio.run(second_service.generate_episode_draft(second.id))

        assert provider.calls == 6
        assert missed.cache_hits == 0
        assert missed.provider_calls == 3
    finally:
        factory.kw["bind"].dispose()


def test_changed_provider_config_hash_is_a_cache_miss(
    app_config_path: Path, tmp_path: Path
) -> None:
    """A provider implementation identity change never reuses semantically stale cached audio."""

    class DifferentFakeTTSProvider(FakeTTSProvider):
        def provider_config_hash(self) -> str:
            return "b" * 64

    factory: sessionmaker[Session] = __import__(
        "editorial_test_support", fromlist=["upgraded_session_factory"]
    ).upgraded_session_factory(app_config_path)
    try:
        shared_provider = FakeTTSProvider()
        first_service, _ = audio_service(factory, shared_provider, tmp_path)
        second_service, _ = audio_service(factory, DifferentFakeTTSProvider(), tmp_path)
        first = episode_for_audio(factory, key="provider-first", day=22)
        second = episode_for_audio(factory, key="provider-second", day=23)

        asyncio.run(first_service.generate_episode_draft(first.id))
        missed = asyncio.run(second_service.generate_episode_draft(second.id))

        assert missed.cache_hits == 0
        assert missed.provider_calls == 3
    finally:
        factory.kw["bind"].dispose()


def test_edge_provider_is_async_and_uses_configured_voice_speed_without_network() -> None:
    """The real provider adapter consumes a mocked async stream, not a live service."""
    calls: list[dict[str, object]] = []

    class FakeCommunicate:
        def __init__(self, text: str, **kwargs: object) -> None:
            calls.append({"text": text, **kwargs})

        async def stream(self):
            yield {"type": "metadata", "data": b"ignored"}
            yield {"type": "audio", "data": b"mp3-a"}
            yield {"type": "audio", "data": b"mp3-b"}

    provider = EdgeTTSProvider(
        timeout_seconds=1,
        max_retries=0,
        communicate_factory=FakeCommunicate,
    )

    result = asyncio.run(provider.synthesize("测试内容", "zh-CN-XiaoxiaoNeural", 1.2, "mp3"))

    assert result.audio_bytes == b"mp3-amp3-b"
    assert result.mime_type == "audio/mpeg"
    assert calls == [
        {
            "text": "测试内容",
            "voice": "zh-CN-XiaoxiaoNeural",
            "rate": "+20%",
            "output_format": "audio-24khz-48kbitrate-mono-mp3",
        }
    ]


def test_edge_provider_timeout_is_bounded_and_does_not_require_network() -> None:
    """Timeout remains a transport concern and exits after the configured retry budget."""

    class BlockingCommunicate:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        async def stream(self):
            await asyncio.sleep(1)
            yield {"type": "audio", "data": b"unreachable"}

    provider = EdgeTTSProvider(
        timeout_seconds=0.001,
        max_retries=0,
        communicate_factory=BlockingCommunicate,
    )

    with pytest.raises(TimeoutError):
        asyncio.run(provider.synthesize("测试内容", "zh-CN-XiaoxiaoNeural", 1.0, "mp3"))


def test_failed_segment_is_retained_and_retry_resumes_from_failure(
    app_config_path: Path, tmp_path: Path
) -> None:
    """A failed call retains early segments; retry synthesizes only pending work."""
    factory: sessionmaker[Session] = __import__(
        "editorial_test_support", fromlist=["upgraded_session_factory"]
    ).upgraded_session_factory(app_config_path)
    try:
        episode = episode_for_audio(factory, key="resume", day=22)
        provider = FakeTTSProvider(fail_on_calls={2})
        service, _ = audio_service(factory, provider, tmp_path)

        with pytest.raises(AudioGenerationError):
            asyncio.run(service.generate_episode_draft(episode.id))

        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            partial = AudioSegmentRepository(unit.session).list_by_episode_revision(
                episode.id, script_revision=1
            )
            assert partial[0].status is AudioSegmentStatus.SUCCEEDED
            assert partial[1].status is AudioSegmentStatus.FAILED
        resumed = asyncio.run(service.generate_episode_draft(episode.id))

        assert provider.calls == 4
        assert resumed.provider_calls == 2
        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            assert all(
                segment.status is AudioSegmentStatus.SUCCEEDED
                for segment in AudioSegmentRepository(unit.session).list_by_episode_revision(
                    episode.id, script_revision=1
                )
            )
    finally:
        factory.kw["bind"].dispose()


def test_generate_audio_pipeline_step_reports_episode_audio_metrics(
    app_config_path: Path, tmp_path: Path
) -> None:
    """The pipeline step exposes Episode, cache, provider, duration, and segment metrics."""
    factory: sessionmaker[Session] = __import__(
        "editorial_test_support", fromlist=["upgraded_session_factory"]
    ).upgraded_session_factory(app_config_path)
    try:
        episode = episode_for_audio(factory, key="pipeline", day=22)
        task_run_id = str(uuid4())
        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            task_run = TaskRunRepository(unit.session).create(
                id=task_run_id,
                task_type="daily_generate",
                business_key=f"audio:{task_run_id}",
                idempotency_key=f"audio:{task_run_id}",
                trigger_type="manual",
                status="running",
                pipeline_version="audio-v1",
                config_fingerprint="a" * 64,
                config_snapshot_json="{}",
                request_json="{}",
                episode_id=episode.id,
            )
            step_id = (
                TaskStepRepository(unit.session)
                .create(
                    task_run_id=task_run.id,
                    step_name="generate_audio",
                    step_order=11,
                    attempt=1,
                    status="running",
                    details_json="{}",
                )
                .id
            )
        service, _ = audio_service(factory, FakeTTSProvider(), tmp_path)
        context = PipelineContext(
            task_run_id=task_run_id,
            session_factory=factory,
            shutdown_requested=asyncio.Event(),
            clock=Clock(),
            values={"active_task_step_id": step_id, "episode_id": episode.id},
        )

        result = asyncio.run(GenerateAudioStep(service).run(context))

        assert result.input_count == 3
        assert result.output_count == 1
        assert result.details["episode_id"] == episode.id
        assert result.details["segment_count"] == 3
        assert result.details["provider_calls"] == 3
        assert result.details["duration_ms"] == 3000
    finally:
        factory.kw["bind"].dispose()
