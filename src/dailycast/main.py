"""Sprint 0 FastAPI application: startup infrastructure and health endpoints only."""

import logging
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from dailycast.core.errors import DailyCastError, InfrastructureError
from dailycast.core.identifiers import UUIDGenerator
from dailycast.core.lifespan import AppRuntime, build_lifespan
from dailycast.core.logging import get_request_id, reset_request_id, set_request_id
from dailycast.core.readiness import evaluate_readiness
from dailycast.db.repositories import PublicationRepository
from dailycast.db.transactions import UnitOfWork

logger = logging.getLogger(__name__)


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

    @app.get("/", tags=["system"])
    async def root() -> dict[str, str]:
        """Identify the HTTP service without exposing any business capability."""
        return {"message": "DailyCast API"}

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
