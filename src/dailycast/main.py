"""Sprint 0 FastAPI application: startup infrastructure and health endpoints only."""

import json
import logging
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from dailycast.core.errors import DailyCastError, InfrastructureError
from dailycast.core.identifiers import UUIDGenerator
from dailycast.core.lifespan import AppRuntime, build_daily_generation_command, build_lifespan
from dailycast.core.logging import get_request_id, reset_request_id, set_request_id
from dailycast.core.readiness import evaluate_readiness
from dailycast.db.models import EpisodeItem, Publication, TaskRun, TriggerType
from dailycast.db.repositories import (
    ArticleRepository,
    EpisodeItemRepository,
    EpisodeRepository,
    PublicationRepository,
    TaskRunRepository,
    TaskStepRepository,
)
from dailycast.db.transactions import UnitOfWork

logger = logging.getLogger(__name__)
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def get_runtime(request: Request) -> AppRuntime:
    """Inject the initialized runtime resources into infrastructure endpoints."""
    runtime = getattr(request.app.state, "runtime", None)
    if not isinstance(runtime, AppRuntime):
        raise InfrastructureError("application runtime is not initialized")
    return runtime


RuntimeDependency = Annotated[AppRuntime, Depends(get_runtime)]


async def dailycast_error_handler(_: Request, error: Exception) -> JSONResponse:
    """Return a stable, non-secret JSON error body."""
    assert isinstance(error, DailyCastError)
    return JSONResponse(
        status_code=error.status_code,
        content={
            "error": {"code": error.code, "message": error.message},
            "request_id": get_request_id(),
        },
    )


def create_app(*, config_path: Path | None = None) -> FastAPI:
    """Create DailyCast diagnostics and immutable public RSS/media resources."""
    app = FastAPI(title="DailyCast API", version="0.1.0", lifespan=build_lifespan(config_path))
    app.add_exception_handler(DailyCastError, dailycast_error_handler)

    @app.middleware("http")
    async def request_id_middleware(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        requested_id = request.headers.get("X-Request-ID")
        request_id = requested_id if requested_id else str(UUIDGenerator().new())
        token = set_request_id(request_id)
        try:
            response = await call_next(request)
        finally:
            reset_request_id(token)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.get("/", response_class=HTMLResponse, tags=["web"])
    async def root(request: Request, runtime: RuntimeDependency) -> HTMLResponse:
        """Render the latest personal episode without exposing draft audio paths."""
        _require_ready(runtime)
        with UnitOfWork(runtime.session_factory) as unit:
            assert unit.session is not None
            episode = EpisodeRepository(unit.session).latest()
            publication = (
                PublicationRepository(unit.session).get_published_for_episode(episode.id)
                if episode is not None
                else None
            )
        return TEMPLATES.TemplateResponse(
            request=request,
            name="home.html",
            context={
                "episode": episode,
                "audio_url": _public_audio_url(publication),
                "duration": _format_audio_duration(
                    episode.actual_duration_ms if episode is not None else None
                ),
                "generation_time": _format_generation_time(
                    episode.generation_time_seconds if episode is not None else None
                ),
            },
        )

    @app.get("/episodes/{episode_id}", response_class=HTMLResponse, tags=["web"])
    async def episode_detail(
        episode_id: int, request: Request, runtime: RuntimeDependency
    ) -> HTMLResponse:
        """Render one persisted editorial snapshot and its published audio, if any."""
        _require_ready(runtime)
        with UnitOfWork(runtime.session_factory) as unit:
            assert unit.session is not None
            episode = EpisodeRepository(unit.session).get(episode_id)
            if episode is None:
                raise HTTPException(status_code=404, detail="episode was not found")
            items = EpisodeItemRepository(unit.session).list_by_episode(episode.id)
            article_ids = _article_ids_from_items(items)
            articles = ArticleRepository(unit.session).list_by_ids(article_ids)
            publication = PublicationRepository(unit.session).get_published_for_episode(episode.id)
            source_count = len({article.source_id for article in articles})
        return TEMPLATES.TemplateResponse(
            request=request,
            name="episode.html",
            context={
                "episode": episode,
                "items": items,
                "source_count": source_count,
                "audio_url": _public_audio_url(publication),
                "duration": _format_audio_duration(episode.actual_duration_ms),
                "generated_at": _format_generated_at(episode.created_at),
            },
        )

    @app.get("/tasks/latest", response_class=HTMLResponse, tags=["web"])
    async def latest_task(request: Request, runtime: RuntimeDependency) -> HTMLResponse:
        """Render one latest TaskRun and its persisted step attempts for daily operation."""
        _require_ready(runtime)
        with UnitOfWork(runtime.session_factory) as unit:
            assert unit.session is not None
            task_run = TaskRunRepository(unit.session).latest()
            steps = (
                TaskStepRepository(unit.session).list_by_task_run(task_run.id)
                if task_run is not None
                else []
            )
        return TEMPLATES.TemplateResponse(
            request=request,
            name="task_latest.html",
            context={
                "task_run": task_run,
                "steps": steps,
                "duration": _format_duration(task_run),
            },
        )

    @app.post("/generate", status_code=202, tags=["web"])
    async def generate(
        runtime: RuntimeDependency,
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key", max_length=256)
        ] = None,
    ) -> JSONResponse:
        """Submit a manual run without letting a previous terminal run block a retry."""
        _require_ready(runtime)
        if runtime.submission_service is None:
            raise InfrastructureError("task submission service is not available")
        command = build_daily_generation_command(runtime.settings, trigger_type=TriggerType.MANUAL)
        # A browser click is a fresh manual attempt. An active task with the same
        # business key is still reused by TaskSubmissionService, while an explicit
        # client key preserves ordinary request-replay idempotency.
        effective_idempotency_key = idempotency_key or f"manual:{UUIDGenerator().new()}"
        task_run = runtime.submission_service.submit(
            replace(command, idempotency_key=effective_idempotency_key)
        )
        return JSONResponse(
            status_code=202,
            content={
                "task_id": task_run.id,
                "status": task_run.status.value,
                "task_url": "/tasks/latest",
            },
        )

    @app.get("/healthz", tags=["system"])
    async def healthz() -> dict[str, str]:
        """Report process liveness without checking external dependencies."""
        return {"status": "ok"}

    @app.get("/readyz", tags=["system"])
    async def readyz(runtime: RuntimeDependency) -> JSONResponse:
        """Report whether configured local dependencies are safe to serve."""
        report = evaluate_readiness(runtime)
        status_code = 200 if report.ready else 503
        return JSONResponse(status_code=status_code, content=report.as_dict())

    @app.get("/feed.xml", tags=["public"])
    async def feed(runtime: RuntimeDependency) -> FileResponse:
        """Serve only atomically published Feed data, never a draft or temporary file."""
        _require_ready(runtime)
        feed_path = runtime.settings.public_dir / "feed.xml"
        if not feed_path.is_file():
            raise HTTPException(status_code=404, detail="RSS feed is not published")
        return FileResponse(feed_path, media_type="application/rss+xml; charset=utf-8")

    @app.get("/media/episodes/{episode_public_id}/{asset_filename}.mp3", tags=["public"])
    async def media(
        episode_public_id: str, asset_filename: str, runtime: RuntimeDependency
    ) -> FileResponse:
        """Serve a durable published asset with ETag and standard byte-range support."""
        _require_ready(runtime)
        valid_asset_name = len(asset_filename) == 64 and all(
            character in "0123456789abcdef" for character in asset_filename
        )
        if not valid_asset_name:
            raise HTTPException(status_code=404, detail="published media asset was not found")
        with UnitOfWork(runtime.session_factory) as unit:
            assert unit.session is not None
            publication = PublicationRepository(unit.session).get_published_by_asset(
                episode_public_id=episode_public_id,
                asset_filename=f"{asset_filename}.mp3",
            )
            if (
                publication is None
                or publication.asset_sha256 is None
                or publication.asset_byte_size is None
            ):
                raise HTTPException(status_code=404, detail="published media asset was not found")
            relative_path = publication.public_asset_path
            expected_sha256 = publication.asset_sha256
            expected_size = publication.asset_byte_size
        if relative_path is None:
            raise HTTPException(status_code=404, detail="published media asset was not found")
        asset_path = (runtime.settings.public_dir / relative_path).resolve()
        try:
            asset_path.relative_to(runtime.settings.public_dir.resolve())
        except ValueError:
            raise HTTPException(
                status_code=404, detail="published media asset was not found"
            ) from None
        if not asset_path.is_file() or asset_path.stat().st_size != expected_size:
            raise HTTPException(status_code=404, detail="published media asset was not found")
        return FileResponse(
            asset_path,
            media_type="audio/mpeg",
            headers={"Accept-Ranges": "bytes", "ETag": f'"{expected_sha256}"'},
        )

    return app


app = create_app()


def _require_ready(runtime: AppRuntime) -> None:
    """Allow public reads only when the configured database revision is safe to query.

    Public RSS/media reads do not require an available TTS provider or FFmpeg binary.  They do,
    however, query ``publications``; therefore they follow the documented schema-mismatch rule
    for every business endpoint while avoiding an unrelated readiness dependency.
    """
    revision = runtime.startup_revision_status
    if runtime.startup_revision_error is not None or revision is None or not revision.is_current:
        raise InfrastructureError("application schema is not ready to serve published resources")


def _article_ids_from_items(items: Sequence[EpisodeItem]) -> tuple[int, ...]:
    """Read frozen EpisodeItem article snapshots defensively for a stable source count."""
    article_ids: set[int] = set()
    for item in items:
        encoded_ids = item.source_article_ids_json
        try:
            values = json.loads(encoded_ids)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(values, list):
            article_ids.update(value for value in values if isinstance(value, int))
    return tuple(sorted(article_ids))


def _public_audio_url(publication: Publication | None) -> str | None:
    """Return only the immutable URL written by a successful RSS publication."""
    asset_path = publication.public_asset_path if publication is not None else None
    if isinstance(asset_path, str) and asset_path.startswith("media/episodes/"):
        return f"/{asset_path}"
    return None


def _format_duration(task_run: TaskRun | None) -> str:
    """Format a completed task duration without inventing a wall-clock value for running work."""
    if task_run is None:
        return "—"
    started_at = task_run.started_at
    ended_at = task_run.ended_at
    if not isinstance(started_at, datetime):
        return "—"
    if not isinstance(ended_at, datetime):
        return "进行中"
    seconds = max(0, round((ended_at - started_at).total_seconds()))
    return f"{seconds} 秒"


def _format_audio_duration(duration_ms: int | None) -> str:
    """Render persisted merged-audio duration consistently in both minimal web pages."""
    if duration_ms is None or duration_ms < 0:
        return "—"
    total_seconds = round(duration_ms / 1000)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def _format_generation_time(seconds: int | None) -> str:
    """Show a persisted task wall-clock metric only after a daily run has completed."""
    return f"{seconds} 秒" if seconds is not None else "—"


def _format_generated_at(value: datetime) -> str:
    """Use one compact, locale-independent timestamp for a persisted Episode snapshot."""
    return value.strftime("%Y-%m-%d %H:%M UTC")
