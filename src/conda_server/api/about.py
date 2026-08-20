"""GET /api/about — version provenance + headline storage stats.

Powers the "About" page in the SPA. Auth-required so the counts don't
leak from a public deployment, but unrestricted within the user base
(no admin gate) — the same kind of info you'd see on a status page.
"""

from __future__ import annotations

import os
import platform
import sys
from typing import Annotated

import rattler
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select

from conda_server import __version__
from conda_server.auth import current_user
from conda_server.config import StorageBackend, get_settings
from conda_server.db import SessionDep
from conda_server.models import (
    Channel,
    ImportJob,
    Package,
    PackageVersion,
    User,
)

router = APIRouter(tags=["about"])


class AboutBuild(BaseModel):
    version: str
    git_sha: str
    build_date: str
    python_version: str
    rattler_version: str
    platform: str


class AboutStats(BaseModel):
    channels: int
    packages: int
    package_versions: int
    total_storage_bytes: int
    import_jobs_total: int
    import_jobs_running: int


class AboutResponse(BaseModel):
    build: AboutBuild
    stats: AboutStats
    # Which object-storage backend the deployment is configured with
    # (local / s3 / azure / gcs). Read-only, non-sensitive — no URLs or
    # credentials. The SPA uses it to tailor the "Show files" CORS hint
    # when a cross-origin fetch to cloud storage is blocked by the
    # browser (see frontend/src/lib/condaFiles.ts).
    storage_backend: StorageBackend


def _rattler_version() -> str:
    # py-rattler doesn't always expose __version__; fall back to the
    # importlib metadata so we don't ship an empty field on bumps that
    # land before upstream wires the dunder.
    try:
        return rattler.__version__  # type: ignore[attr-defined]
    except AttributeError:
        try:
            from importlib.metadata import version

            return version("py-rattler")
        except Exception:
            return "unknown"


@router.get("/about", response_model=AboutResponse)
async def about(
    session: SessionDep,
    user: Annotated[User, Depends(current_user)],
) -> AboutResponse:
    _ = user  # auth-only, no per-user data

    build = AboutBuild(
        version=__version__,
        git_sha=os.getenv("CONDA_SERVER_GIT_SHA", "unknown"),
        build_date=os.getenv("CONDA_SERVER_BUILD_DATE", "unknown"),
        python_version=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        rattler_version=_rattler_version(),
        platform=f"{platform.system()} {platform.machine()}",
    )

    # Single round-trip per count. Total storage is the sum of
    # PackageVersion.size; null sizes (rare — older rows pre-size
    # column) are ignored by SUM.
    channels = await session.scalar(select(func.count(Channel.id))) or 0
    packages = await session.scalar(select(func.count(Package.id))) or 0
    versions = await session.scalar(select(func.count(PackageVersion.id))) or 0
    total_bytes = await session.scalar(select(func.coalesce(func.sum(PackageVersion.size), 0))) or 0
    jobs_total = await session.scalar(select(func.count(ImportJob.id))) or 0
    jobs_running = (
        await session.scalar(
            select(func.count(ImportJob.id)).where(ImportJob.status.in_(["pending", "running"]))
        )
        or 0
    )

    stats = AboutStats(
        channels=int(channels),
        packages=int(packages),
        package_versions=int(versions),
        total_storage_bytes=int(total_bytes),
        import_jobs_total=int(jobs_total),
        import_jobs_running=int(jobs_running),
    )

    return AboutResponse(
        build=build,
        stats=stats,
        storage_backend=get_settings().storage.backend,
    )
