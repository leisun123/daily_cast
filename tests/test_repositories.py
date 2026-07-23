"""Repository and UnitOfWork behavior for the V1 database layer."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from alembic import command

from dailycast.core.config import load_settings
from dailycast.db.models import ArticleStatus, EpisodeStatus, TaskRunStatus
from dailycast.db.repositories import (
    ArticleRepository,
    AudioSegmentRepository,
    EpisodeRepository,
    LLMArtifactRepository,
    NewsEventRepository,
    PublicationRepository,
    SourceRepository,
    TaskRunRepository,
    TaskStepRepository,
)
from dailycast.db.revision import build_alembic_config
from dailycast.db.session import create_session_factory, create_sqlite_engine
from dailycast.db.transactions import UnitOfWork


def now() -> datetime:
    """Return a UTC timestamp for repository inputs."""
    return datetime.now(UTC)


def upgraded_factory(app_config_path: Path):
    """Create an Alembic-upgraded session factory for repository tests."""
    settings = load_settings(config_path=app_config_path)
    config = build_alembic_config(
        ini_path=Path(__file__).resolve().parents[1] / "alembic.ini",
        database_url=settings.database.url,
    )
    command.upgrade(config, "head")
    engine = create_sqlite_engine(settings.database)
    return engine, create_session_factory(engine)


def test_repositories_create_and_query_v1_records(app_config_path: Path) -> None:
    """Repositories persist their documented minimum CRUD and cache operations."""
    engine, factory = upgraded_factory(app_config_path)
    timestamp = now()
    try:
        with UnitOfWork(factory) as uow:
            assert uow.session is not None
            sources = SourceRepository(uow.session)
            source = sources.create(
                id="repo-source",
                name="Repository Source",
                kind="rss",
                entry_url="https://repo.example/feed.xml",
                normalized_entry_url="https://repo.example/feed.xml",
                config_json="{}",
            )
            assert sources.get(source.id) is source
            assert sources.list() == [source]
            sources.update(source, name="Renamed Source", priority=80)
            sources.disable(source)
            assert source.enabled is False

            articles = ArticleRepository(uow.session)
            article = articles.upsert(
                source_id=source.id,
                url="https://repo.example/article",
                normalized_url="https://repo.example/article",
                url_hash="a" * 64,
                title="Repository article",
                normalized_title="repository article",
                title_hash="b" * 64,
                discovered_at=timestamp,
                status=ArticleStatus.DISCOVERED,
                metadata_json="{}",
            )
            assert articles.get_by_url_hash("a" * 64) is article
            assert articles.list() == [article]

            events = NewsEventRepository(uow.session)
            event = events.create(
                event_key=f"2026-07-22:{article.id}",
                event_date=date(2026, 7, 22),
                representative_article_id=article.id,
                title=article.title,
                status="candidate",
                risk_flags_json="[]",
                cluster_algorithm="tfidf_char",
                cluster_version="1",
                cluster_threshold=0.58,
                cluster_signature="c" * 64,
            )
            assert events.get(event.id) is event

            episodes = EpisodeRepository(uow.session)
            episode = episodes.create(
                public_id="44444444-4444-4444-4444-444444444444",
                episode_date=date(2026, 7, 22),
            )
            episodes.update_status(episode, EpisodeStatus.REVIEW_REQUIRED)
            assert episode.status is EpisodeStatus.REVIEW_REQUIRED

            task_runs = TaskRunRepository(uow.session)
            task_run = task_runs.create(
                id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                task_type="daily_generate",
                business_key="daily:2026-07-22:daily",
                idempotency_key="repo-task",
                trigger_type="manual",
                status=TaskRunStatus.QUEUED,
                pipeline_version="1",
                config_fingerprint="d" * 64,
                config_snapshot_json="{}",
                request_json="{}",
                episode_id=episode.id,
            )
            assert task_runs.get_active_by_business_key(task_run.business_key) is task_run
            task_runs.update_status(task_run, TaskRunStatus.RUNNING)
            task_runs.update_heartbeat(task_run, timestamp)

            steps = TaskStepRepository(uow.session)
            step = steps.create(
                task_run_id=task_run.id,
                step_name="ranking",
                step_order=1,
                attempt=1,
                status="running",
                details_json="{}",
            )
            steps.finish(step, status="succeeded", ended_at=timestamp, output_count=1)

            artifacts = LLMArtifactRepository(uow.session)
            artifact = artifacts.insert_validated(
                operation="score_events",
                provider="openai_compatible",
                model="model",
                prompt_version="1",
                schema_version="1",
                generation_config_hash="e" * 64,
                input_hash="f" * 64,
                output_json="{}",
                output_hash="0" * 64,
                created_by_task_run_id=task_run.id,
                created_by_task_step_id=step.id,
            )
            assert (
                artifacts.get_by_cache_identity(
                    operation="score_events",
                    provider="openai_compatible",
                    model="model",
                    prompt_version="1",
                    schema_version="1",
                    generation_config_hash="e" * 64,
                    input_hash="f" * 64,
                )
                is artifact
            )
            assert artifacts.prune_before(timestamp - timedelta(days=1)) == 0

            segments = AudioSegmentRepository(uow.session)
            segment = segments.create(
                episode_id=episode.id,
                script_revision=0,
                segment_index=0,
                segmenter_version="1",
                text="测试",
                text_hash="1" * 64,
                cache_key="2" * 64,
                provider="openai_compatible",
                model="tts-1",
                voice="alloy",
                provider_config_hash="3" * 64,
                status="succeeded",
            )
            assert segments.get_by_cache_key("2" * 64, "3" * 64) is segment

            publications = PublicationRepository(uow.session)
            publication = publications.create(
                episode_id=episode.id,
                publisher_type="rss",
                target_key="self-hosted",
                status="pending",
                idempotency_key="publication-1",
                request_fingerprint="4" * 64,
            )
            assert publications.get_by_target(episode.id, "rss", "self-hosted") is publication

        with UnitOfWork(factory) as uow:
            assert uow.session is not None
            segments = AudioSegmentRepository(uow.session)
            assert segments.get_by_cache_key("2" * 64, "3" * 64) is not None
    finally:
        engine.dispose()


def test_unit_of_work_rolls_back_on_exception(app_config_path: Path) -> None:
    """A failed unit of work does not commit a Source row."""
    engine, factory = upgraded_factory(app_config_path)
    try:
        try:
            with UnitOfWork(factory) as uow:
                assert uow.session is not None
                SourceRepository(uow.session).create(
                    id="rolled-back-source",
                    name="Rolled back",
                    kind="rss",
                    entry_url="https://rollback.example/feed.xml",
                    normalized_entry_url="https://rollback.example/feed.xml",
                    config_json="{}",
                )
                raise RuntimeError("rollback")
        except RuntimeError:
            pass

        with UnitOfWork(factory) as uow:
            assert uow.session is not None
            assert SourceRepository(uow.session).get("rolled-back-source") is None
    finally:
        engine.dispose()
