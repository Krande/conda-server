from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from conda_server.auth import current_user_optional, visible_channel_or_404
from conda_server.db import SessionDep
from conda_server.mirror_listing import cached_package, cached_packages
from conda_server.models import Channel, Package, PackageVersion, User
from conda_server.storage import get_storage
from conda_server.versions import sort_versions, version_ranks

router = APIRouter(prefix="/channels/{channel_name}/packages", tags=["packages"])


class PackageVersionOut(BaseModel):
    version: str
    build: str
    build_number: int
    subdir: str
    filename: str
    size: int | None
    sha256: str | None
    md5: str | None = None
    # Package match-specs, straight from repodata. Example entries:
    # "numpy >=1.20", "openssl >=3.5.5,<4.0a0", "ucrt". The UI parses the
    # leading name for linking and shows the rest as a version constraint.
    depends: list[str] = []
    # Same shape as ``depends``; soft constraints the solver considers
    # without requiring the package (runtime-compat hints).
    constrains: list[str] = []
    license: str | None = None
    # Upstream-set millisecond epoch; null for locally-built archives
    # that predate the convention.
    timestamp: int | None = None
    # Set when this version was pulled in via the import-from-upstream
    # flow; null for plain admin uploads.
    imported_from: str | None = None
    # When the artifact landed on *this* server (not when it was built —
    # that's ``timestamp``). For mirror channels there's no row to read,
    # so it's the storage object's last-modified instead. Null when
    # neither is available.
    created_at: datetime | None = None
    # Dense rank of this artifact's version within the package, 0 =
    # newest. Lets the UI re-sort by version without reimplementing
    # conda's ordering rules in the browser; see conda_server.versions.
    version_order: int = 0

    model_config = {"from_attributes": True}


class PackageOut(BaseModel):
    name: str
    description: str | None
    versions: list[PackageVersionOut]


def _version_out(v: Any, version_order: int) -> PackageVersionOut:
    """Flatten a package-version row to the outbound shape.

    Takes ``Any`` rather than ``PackageVersion`` because mirror channels
    have no DB rows — the listing hands us a filename-derived dataclass
    with the same core attributes but none of the repodata extras. The
    ``getattr`` defaults are what bridge the two.

    ``depends`` / ``constrains`` live on dedicated columns; ``license``
    and ``timestamp`` live inside the repodata ``info`` JSON blob, so we
    pull them out here rather than bolt a reflection layer onto the ORM.
    """
    info = getattr(v, "info", None) or {}
    return PackageVersionOut(
        version=v.version,
        build=v.build,
        build_number=v.build_number,
        subdir=v.subdir,
        filename=v.filename,
        size=v.size,
        sha256=v.sha256,
        md5=getattr(v, "md5", None),
        depends=list(getattr(v, "depends", None) or []),
        constrains=list(getattr(v, "constrains", None) or []),
        license=info.get("license") if isinstance(info, dict) else None,
        timestamp=info.get("timestamp") if isinstance(info, dict) else None,
        imported_from=getattr(v, "imported_from", None),
        created_at=getattr(v, "created_at", None),
        version_order=version_order,
    )


def _versions_out(versions: Iterable[Any]) -> list[PackageVersionOut]:
    """Order artifacts newest-version-first and tag each with its rank.

    Sorting here rather than in SQL is deliberate: conda version order
    isn't expressible as an ORDER BY (it needs segment-wise numeric
    comparison plus epoch/post/dev rules), and the endpoint already
    materialises every version of the package to serialize it, so there
    is nothing extra to fetch. The expensive case — a channel with tens
    of thousands of *packages* — is bounded by the per-package version
    count, not the channel size.
    """
    ordered = sort_versions(list(versions))
    ranks = version_ranks(ordered)
    return [_version_out(v, rank) for v, rank in zip(ordered, ranks, strict=True)]


def _package_out(pkg: Package) -> PackageOut:
    return PackageOut(
        name=pkg.name,
        description=pkg.description,
        versions=_versions_out(pkg.versions),
    )


@router.get("", response_model=list[PackageOut])
async def list_packages(
    channel_name: str,
    session: SessionDep,
    user: Annotated[User | None, Depends(current_user_optional)],
):
    channel = await visible_channel_or_404(session, channel_name, user)
    if channel.mirror_url:
        return await _mirror_package_list(channel)
    # Eager-load versions — pydantic's response serializer runs outside the
    # async context and can't trigger a lazy load (greenlet_spawn error).
    result = await session.execute(
        select(Package)
        .options(selectinload(Package.versions))
        .where(Package.channel_id == channel.id)
        .order_by(Package.name)
    )
    return [_package_out(p) for p in result.scalars()]


@router.get("/{name}", response_model=PackageOut)
async def get_package(
    channel_name: str,
    name: str,
    session: SessionDep,
    user: Annotated[User | None, Depends(current_user_optional)],
):
    channel = await visible_channel_or_404(session, channel_name, user)
    if channel.mirror_url:
        pkg = await cached_package(get_storage(), channel, name)
        if pkg is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="package not found")
        return PackageOut(
            name=pkg.name,
            description=pkg.description,
            versions=_versions_out(pkg.versions),
        )

    result = await session.execute(
        select(Package)
        .options(selectinload(Package.versions))
        .where(Package.channel_id == channel.id, Package.name == name)
    )
    pkg_row = result.scalar_one_or_none()
    if pkg_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="package not found")
    return _package_out(pkg_row)


async def _mirror_package_list(channel: Channel) -> list[PackageOut]:
    raw = await cached_packages(get_storage(), channel)
    return [
        PackageOut(
            name=pkg.name,
            description=pkg.description,
            versions=_versions_out(pkg.versions),
        )
        for pkg in raw
    ]


# Silence unused-import warning; PackageVersion is used indirectly via relationship.
_ = PackageVersion
