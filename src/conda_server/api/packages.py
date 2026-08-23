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
    # Long-form ``about.json`` description of the metadata-bearing
    # version (see ``_about_source``), falling back to the operator-set
    # ``Package.description`` column when the archives carry nothing.
    description: str | None
    # One-line ``about.json`` summary. Separate from ``description``
    # because they are separate recipe fields with different lengths —
    # the page leads with the summary and keeps the description below it.
    summary: str | None = None
    # Links from the recipe. Null whenever the archive did not declare
    # one; the page renders no button rather than an empty href.
    doc_url: str | None = None
    home: str | None = None
    dev_url: str | None = None
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


def _versions_out(ordered: list[Any]) -> list[PackageVersionOut]:
    """Tag each already-ordered artifact with its dense version rank.

    Callers pass the output of ``sort_versions``. Sorting there rather
    than in SQL is deliberate: conda version order isn't expressible as
    an ORDER BY (it needs segment-wise numeric comparison plus
    epoch/post/dev rules), and the endpoint already materialises every
    version of the package to serialize it, so there is nothing extra to
    fetch. The expensive case — a channel with tens of thousands of
    *packages* — is bounded by the per-package version count, not the
    channel size.
    """
    ranks = version_ranks(ordered)
    return [_version_out(v, rank) for v, rank in zip(ordered, ranks, strict=True)]


#: Version-row attributes that carry ``about.json`` metadata. A row is a
#: usable metadata source when at least one of them is set.
_ABOUT_FIELDS = ("doc_url", "home", "dev_url", "summary", "description")


def _about_source(ordered: list[Any]) -> Any | None:
    """Pick the version whose ``about.json`` represents the package.

    The archive stores this metadata per artifact, but the page shows one
    docs link per package, so something has to decide which version wins.
    It is the **newest version by conda ordering** — the same rule the
    version table and the recent-uploads list already use — and
    explicitly *not* the most recently uploaded row. Those two disagree
    more often than they look like they would: a rebuild of an older
    version can land after a newer one has already shipped, and ordering
    by upload date would then advertise the older release's links as the
    package's.

    ``ordered`` is already newest-first, so this is a scan rather than a
    second sort: the first version carrying any metadata wins. Versions
    with nothing at all are skipped rather than winning and blanking the
    page. Showing an older release's docs link beats showing none, and
    among versions that *do* carry metadata conda ordering still decides,
    so this never silently prefers a stale link over a fresh one.

    That skip is now defence rather than the common path. The indexer
    captures metadata for the newest version only — precisely because
    this function reads no other — so on a channel indexed by a current
    server the first version scanned is normally the one that answers.
    It still earns its place: rows captured by earlier servers, and rows
    filled in by ``conda_server.backfill`` (which inspects every version,
    not just the newest), both put metadata on older versions, and a
    newest version whose recipe simply omitted ``about.json`` is a real
    and permanent case rather than a transitional one.
    """
    for version in ordered:
        if any(getattr(version, field, None) for field in _ABOUT_FIELDS):
            return version
    return None


def _package_out(pkg: Package) -> PackageOut:
    return _package_out_from(pkg.name, pkg.description, pkg.versions)


def _package_out_from(
    name: str,
    fallback_description: str | None,
    versions: Iterable[Any],
) -> PackageOut:
    """Build the outbound package shape, resolving about.json metadata.

    Takes loose arguments rather than a ``Package`` because mirror
    channels have no rows — their listing is a filename-derived dataclass
    with no metadata columns, so ``_about_source`` finds nothing and every
    link comes back null, which is correct: we never opened those
    archives.
    """
    ordered = sort_versions(list(versions))
    source = _about_source(ordered)
    return PackageOut(
        name=name,
        description=(getattr(source, "description", None) if source else None)
        or fallback_description,
        summary=getattr(source, "summary", None) if source else None,
        doc_url=getattr(source, "doc_url", None) if source else None,
        home=getattr(source, "home", None) if source else None,
        dev_url=getattr(source, "dev_url", None) if source else None,
        versions=_versions_out(ordered),
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
        return _package_out_from(pkg.name, pkg.description, pkg.versions)

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
    return [_package_out_from(pkg.name, pkg.description, pkg.versions) for pkg in raw]


# Silence unused-import warning; PackageVersion is used indirectly via relationship.
_ = PackageVersion
