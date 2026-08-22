"""Cross-channel search.

The results respect channel visibility — anonymous callers see hits only
in public channels, authenticated callers add channels they're members
of, server admins see everything. Search is naive substring-match on
``Package.name`` and ``Channel.name``; good enough for the "find a
package across my channels" use case. Mirror channels with an empty
Package table (we derive their listing from storage) naturally don't
show package hits — upstream is authoritative and clients should go
there for exhaustive search.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from conda_server.auth import current_user_optional, visible_channels_stmt
from conda_server.db import SessionDep
from conda_server.models import Channel, Package, PackageVersion, User
from conda_server.versions import sort_versions

router = APIRouter(prefix="/search", tags=["search"])

_MIN_QUERY_LENGTH = 2
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100

_DEFAULT_RECENT_LIMIT = 5
_MAX_RECENT_LIMIT = 50


_MAX_RESOLVE_NAMES = 100


@router.get("/resolve")
async def resolve_packages(
    session: SessionDep,
    user: Annotated[User | None, Depends(current_user_optional)],
    names: str = Query(
        ...,
        description="Comma-separated package names to resolve. Capped at 100.",
    ),
) -> dict[str, dict[str, str]]:
    """Look up which (non-mirror) channel each name lives in, if any.

    Designed for the PackageDetail page's dependency links: for every
    dep in a version's ``depends`` / ``constrains``, the UI wants to
    link to our own local channel page if the package is indexed here
    rather than kicking off to conda-forge. Mirror channels are skipped
    because they derive their listing from storage filenames rather than
    the Package table — a name may be "present" upstream but we have no
    row to point at.

    Returns ``{name: {channel: <name>}}`` for resolved names; unresolved
    names are omitted from the map (null-by-absence is smaller on the
    wire than explicit null entries).
    """
    name_list: list[str] = []
    seen: set[str] = set()
    for raw in names.split(","):
        n = raw.strip()
        if n and n not in seen:
            seen.add(n)
            name_list.append(n)
            if len(name_list) >= _MAX_RESOLVE_NAMES:
                break
    if not name_list:
        return {}

    visible = visible_channels_stmt(user).subquery()
    result = await session.execute(
        select(
            Package.name.label("pkg"),
            visible.c.name.label("channel"),
        )
        .join(visible, Package.channel_id == visible.c.id)
        .where(
            Package.name.in_(name_list),
            visible.c.mirror_url.is_(None),
        )
        .order_by(visible.c.name)
    )
    out: dict[str, dict[str, str]] = {}
    for row in result:
        if row.pkg not in out:
            out[row.pkg] = {"channel": row.channel}
    return out


@router.get("/recent")
async def recent_uploads(
    session: SessionDep,
    user: Annotated[User | None, Depends(current_user_optional)],
    limit: int = Query(_DEFAULT_RECENT_LIMIT, ge=1, le=_MAX_RECENT_LIMIT),
) -> list[dict[str, Any]]:
    """Packages with the most recent upload activity, one row per package.

    Two separate questions live in this endpoint, and conflating them was
    a bug:

    *Which* packages appear, and in what order, is a recency question —
    ranked by the newest ``created_at`` among the package's versions
    (when a version landed *here*, not the upstream build timestamp).

    *Which version* each row advertises is a version-ordering question,
    and it has to be answered the same way the channel listing and the
    package page answer it: the newest version by conda ordering (see
    ``conda_server.versions``), not whichever row happens to have the
    latest ``created_at``. Those two can disagree — an older version can
    land after a newer one, e.g. when a recipe moves to ``noarch`` and a
    build on the pre-bump merge commit republishes 2.4.0 as a noarch
    artifact hours after 2.4.1 already shipped. Picking by ``created_at``
    then advertised 2.4.0 as the package's version while every other
    view said 2.4.1.

    The timestamp reported is the displayed version's own ``created_at``,
    so the row describes exactly one artifact rather than pairing one
    version's number with another's upload time.

    Mirror channels have no ``PackageVersion`` rows — their listing is
    derived from storage — so they naturally don't appear here.
    """
    visible = visible_channels_stmt(user).subquery()

    # Rank packages in SQL so the limit is exact and the ordering is
    # deterministic. ``Package.id`` breaks created_at ties, which are real:
    # a batch import stamps every row it creates with the same timestamp,
    # and without a tiebreak the winner was whatever order the DB returned.
    recency = (
        select(
            Package.id.label("package_id"),
            Package.name.label("name"),
            visible.c.name.label("channel"),
            func.max(PackageVersion.created_at).label("last_upload"),
        )
        .join(Package, PackageVersion.package_id == Package.id)
        .join(visible, Package.channel_id == visible.c.id)
        .where(visible.c.mirror_url.is_(None))
        .group_by(Package.id, Package.name, visible.c.name)
        .order_by(func.max(PackageVersion.created_at).desc(), Package.id.desc())
        .limit(limit)
    )
    ranked = (await session.execute(recency)).all()
    if not ranked:
        return []

    # Second pass for the version each row shows. Only the already-limited
    # set of packages is loaded (at most ``limit`` of them, capped at 50),
    # so this stays bounded by per-package version count rather than by
    # channel size — the same trade-off the package endpoints make, and for
    # the same reason: conda ordering isn't expressible as an ORDER BY.
    rows = (
        (
            await session.execute(
                select(PackageVersion).where(
                    PackageVersion.package_id.in_([r.package_id for r in ranked])
                )
            )
        )
        .scalars()
        .all()
    )
    by_package: dict[int, list[PackageVersion]] = {}
    for row in rows:
        by_package.setdefault(row.package_id, []).append(row)

    out: list[dict[str, Any]] = []
    for entry in ranked:
        versions = by_package.get(entry.package_id)
        if not versions:  # pragma: no cover - group-by guarantees at least one
            continue
        newest = sort_versions(versions)[0]
        created: datetime | None = newest.created_at
        out.append(
            {
                "name": entry.name,
                "channel": entry.channel,
                "version": newest.version,
                "subdir": newest.subdir,
                "created_at": created.isoformat() if created is not None else None,
            }
        )
    return out


@router.get("")
async def search(
    session: SessionDep,
    user: Annotated[User | None, Depends(current_user_optional)],
    q: str = Query(..., min_length=0, max_length=128),
    limit: int = Query(_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
) -> dict[str, list[dict[str, Any]]]:
    """Substring-match channels and packages.

    Empty or too-short queries return empty lists so typing in the UI
    doesn't spam the server with one-char searches.
    """
    q_clean = q.strip()
    if len(q_clean) < _MIN_QUERY_LENGTH:
        return {"packages": [], "channels": []}

    pattern = f"%{q_clean}%"
    visible = visible_channels_stmt(user).subquery()

    pkg_result = await session.execute(
        select(
            Package.name.label("name"),
            visible.c.name.label("channel"),
        )
        .join(visible, Package.channel_id == visible.c.id)
        .where(func.lower(Package.name).like(func.lower(pattern)))
        .order_by(Package.name)
        .limit(limit)
    )
    packages = [{"name": row.name, "channel": row.channel} for row in pkg_result]

    ch_result = await session.execute(
        select(Channel)
        .join(visible, Channel.id == visible.c.id)
        .where(func.lower(Channel.name).like(func.lower(pattern)))
        .order_by(Channel.name)
        .limit(limit)
    )
    channels = [
        {
            "name": c.name,
            "description": c.description,
            "private": c.private,
            "mirror_url": c.mirror_url,
        }
        for c in ch_result.scalars()
    ]

    return {"packages": packages, "channels": channels}
