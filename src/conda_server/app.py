from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from conda_server import __version__
from conda_server.api import about as about_api
from conda_server.api import audit as audit_api
from conda_server.api import auth as auth_api
from conda_server.api import channels, health, packages, repodata, search, upstream
from conda_server.cleanup import cleanup_loop
from conda_server.config import get_settings
from conda_server.db import dispose_engine
from conda_server.logging import configure_logging
from conda_server.metrics import metrics_endpoint


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await _fail_orphaned_import_jobs()
    cleanup_task = asyncio.create_task(cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task
        await dispose_engine()


async def _fail_orphaned_import_jobs() -> None:
    """Mark any ``running`` import jobs as failed at startup.

    ImportJob runners are in-process asyncio tasks, not durable workers.
    A pod restart mid-import leaves their rows in ``running`` forever
    and the UI's status poll spins indefinitely. Sweep them on boot so
    operators see "pod restarted, please retry" instead of a stuck bar.
    """
    from datetime import datetime

    from sqlalchemy import update

    from conda_server.db import get_sessionmaker
    from conda_server.models import ImportJob

    sm = get_sessionmaker()
    async with sm() as session:
        await session.execute(
            update(ImportJob)
            .where(ImportJob.status.in_(["pending", "running"]))
            .values(
                status="failed",
                error="pod restarted before the import finished",
                finished_at=datetime.now(UTC),
            )
        )
        await session.commit()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.logging)

    app = FastAPI(
        title="conda-server",
        version=__version__,
        description="A modern, open-source conda package server built on the rattler ecosystem",
        lifespan=lifespan,
    )

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.auth.session_secret,
        https_only=settings.auth.session_https_only,
    )

    app.include_router(health.router)
    # Prometheus scrape target. Registered before the SPA fallback so it
    # doesn't get swallowed by the index.html route.
    app.get("/metrics", include_in_schema=False)(metrics_endpoint)
    app.include_router(auth_api.router)
    app.include_router(channels.router, prefix="/api")
    app.include_router(packages.router, prefix="/api")
    app.include_router(search.router, prefix="/api")
    app.include_router(audit_api.router, prefix="/api")
    app.include_router(upstream.router, prefix="/api")
    app.include_router(about_api.router, prefix="/api")
    app.include_router(repodata.router)

    _mount_frontend(app)

    return app


def _mount_frontend(app: FastAPI) -> None:
    """Serve the built SPA from ``frontend/dist`` if it exists.

    Falls back to ``index.html`` for client-side routes so a deep link like
    ``/channels/foo`` works after a page refresh. Package / repodata paths
    are handled by the repodata router before this fallback runs.
    """
    candidates = [
        Path(__file__).resolve().parents[2] / "frontend" / "dist",
        Path.cwd() / "frontend" / "dist",
        Path("/app/frontend/dist"),
    ]
    dist = next((p for p in candidates if p.is_dir()), None)
    if dist is None:
        return

    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    for static_name in ("favicon.svg", "favicon.ico", "robots.txt"):
        candidate = dist / static_name

        def _make_handler(path: Path) -> callable:  # closure over a fresh path
            async def _handler() -> FileResponse:
                return FileResponse(path)

            return _handler

        if candidate.is_file():
            app.get(f"/{static_name}", include_in_schema=False)(_make_handler(candidate))

    index = dist / "index.html"
    if not index.is_file():
        return

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(request: Request, full_path: str) -> FileResponse:
        # API, docs, and auth paths never reach here — their routers are
        # registered first. Anything else that doesn't look like a file we
        # serve is treated as a client-side route.
        if full_path.startswith(("api/", "docs", "redoc", "openapi.json")):
            raise HTTPException(status_code=404)
        # Conda-shaped requests that slipped past the repodata route (e.g.
        # ``/some-channel/made-up-subdir/repodata.json``): any trailing
        # filename-looking segment with a dot. These clients want bytes,
        # not index.html.
        last = full_path.rsplit("/", 1)[-1]
        if "." in last and "/" in full_path:
            raise HTTPException(status_code=404)
        _ = request  # silence unused arg lint
        return FileResponse(index)
