"""Server-admin read endpoint for the audit log.

Paging is intentionally coarse — the table is write-once, read-rarely.
Filters cover the dimensions ops actually uses (by actor, by channel, by
action). For anything fancier, someone should go query Postgres directly.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select

from conda_server.audit import ACTIONS
from conda_server.auth import require_admin
from conda_server.db import SessionDep
from conda_server.models import AuditLog, User

router = APIRouter(prefix="/admin/audit", tags=["admin"])

_DEFAULT_LIMIT = 100
_MAX_LIMIT = 500


class AuditEntry(BaseModel):
    id: int
    actor_id: int | None
    actor_email: str | None
    action: str
    channel_name: str | None
    target: str | None
    meta: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


@router.get("", response_model=list[AuditEntry])
async def list_audit(
    session: SessionDep,
    _admin: Annotated[User, Depends(require_admin)],
    action: str | None = Query(None, description="Filter by exact action name"),
    channel: str | None = Query(None, description="Filter by channel name"),
    actor: str | None = Query(None, description="Filter by actor email (exact match)"),
    limit: int = Query(_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    offset: int = Query(0, ge=0),
) -> list[AuditLog]:
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc())
    if action is not None:
        if action not in ACTIONS:
            return []
        stmt = stmt.where(AuditLog.action == action)
    if channel is not None:
        stmt = stmt.where(AuditLog.channel_name == channel)
    if actor is not None:
        stmt = stmt.where(AuditLog.actor_email == actor)
    stmt = stmt.limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars())


@router.get("/actions", response_model=list[str])
async def list_actions(
    _admin: Annotated[User, Depends(require_admin)],
) -> list[str]:
    """The canonical set of action strings used for filtering."""
    return sorted(ACTIONS)
