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

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from conda_server.auth import current_user_optional, visible_channels_stmt
from conda_server.db import SessionDep
from conda_server.models import Channel, Package, User

router = APIRouter(prefix="/search", tags=["search"])

_MIN_QUERY_LENGTH = 2
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100


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
