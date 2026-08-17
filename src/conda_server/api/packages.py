from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from conda_server.auth import current_user_optional, visible_channel_or_404
from conda_server.db import SessionDep
from conda_server.mirror_listing import cached_package, cached_packages
from conda_server.models import Channel, Package, PackageVersion, User
from conda_server.storage import get_storage

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

    model_config = {"from_attributes": True}


class PackageOut(BaseModel):
    name: str
    description: str | None
    versions: list[PackageVersionOut]


def _version_out(v: PackageVersion) -> PackageVersionOut:
    """Flatten a PackageVersion row to the outbound shape.

    ``depends`` / ``constrains`` live on dedicated columns; ``license``
    and ``timestamp`` live inside the repodata ``info`` JSON blob, so we
    pull them out here rather than bolt a reflection layer onto the ORM.
    """
    info = v.info or {}
    return PackageVersionOut(
        version=v.version,
        build=v.build,
        build_number=v.build_number,
        subdir=v.subdir,
        filename=v.filename,
        size=v.size,
        sha256=v.sha256,
        md5=v.md5,
        depends=list(v.depends or []),
        constrains=list(v.constrains or []),
        license=info.get("license") if isinstance(info, dict) else None,
        timestamp=info.get("timestamp") if isinstance(info, dict) else None,
        imported_from=v.imported_from,
    )


def _package_out(pkg: Package) -> PackageOut:
    return PackageOut(
        name=pkg.name,
        description=pkg.description,
        versions=[_version_out(v) for v in pkg.versions],
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
            versions=[
                PackageVersionOut.model_validate(v, from_attributes=True) for v in pkg.versions
            ],
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
            versions=[
                PackageVersionOut.model_validate(v, from_attributes=True) for v in pkg.versions
            ],
        )
        for pkg in raw
    ]


# Silence unused-import warning; PackageVersion is used indirectly via relationship.
_ = PackageVersion
