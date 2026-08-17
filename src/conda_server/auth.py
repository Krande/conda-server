from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from conda_server.config import get_settings
from conda_server.db import SessionDep
from conda_server.models import ApiKey, Channel, ChannelMember, User

TOKEN_PREFIX = "cs_"

# Ordered from least to most privileged. Server-level admin outranks any
# per-channel role and is tracked separately as "admin".
ChannelRole = Literal["reader", "writer", "owner"]
AccessLevel = Literal["reader", "writer", "owner", "admin"]

_LEVEL_RANK: dict[str, int] = {"reader": 1, "writer": 2, "owner": 3, "admin": 4}


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_token() -> str:
    """Mint a bearer token. Prefixed so leaked tokens are grep-friendly."""
    return f"{TOKEN_PREFIX}{secrets.token_urlsafe(32)}"


async def current_user_optional(request: Request, session: SessionDep) -> User | None:
    """Resolve a User from either a session cookie or a bearer token.

    Returns ``None`` when neither is present or valid. Expired API keys are
    treated as absent.
    """
    token = _extract_bearer(request)
    if token:
        result = await session.execute(select(ApiKey).where(ApiKey.key_hash == hash_token(token)))
        api_key = result.scalar_one_or_none()
        if api_key is not None and not _is_expired(api_key):
            return await session.get(User, api_key.user_id)

    subject = request.session.get("sub") if _has_session(request) else None
    if subject:
        result = await session.execute(select(User).where(User.subject == subject))
        return result.scalar_one_or_none()

    return None


def _is_expired(key: ApiKey) -> bool:
    if key.expires_at is None:
        return False
    expires_at = key.expires_at
    # SQLite strips timezone info; assume any naive datetime in the DB is UTC.
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= datetime.now(UTC)


async def current_user(
    user: Annotated[User | None, Depends(current_user_optional)],
) -> User:
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return user


async def require_admin(user: Annotated[User, Depends(current_user)]) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin required")
    return user


async def channel_access_level(
    session: AsyncSession,
    channel: Channel,
    user: User | None,
) -> AccessLevel | None:
    """Compute the caller's effective permission on this channel.

    Precedence:
    - Server admin → ``"admin"`` (bypasses per-channel membership entirely).
    - Explicit per-user membership → that row's role.
    - Public channel → ``"reader"`` for anyone (including anon).
    - Private channel with no membership → ``None`` (404 at the API boundary).

    Group memberships (``ChannelMember.group_name``) are reserved in the
    schema but not consulted yet — that waits for Authentik group claims
    in the OIDC token to be plumbed through.
    """
    if user is not None and user.role == "admin":
        return "admin"

    member_level: ChannelRole | None = None
    if user is not None:
        result = await session.execute(
            select(ChannelMember).where(
                ChannelMember.channel_id == channel.id,
                ChannelMember.user_id == user.id,
            )
        )
        member = result.scalar_one_or_none()
        if member is not None:
            member_level = member.role  # type: ignore[assignment]

    if not channel.private:
        # Public: at least "reader" for everyone. Membership upgrades.
        if member_level is None or _LEVEL_RANK[member_level] <= _LEVEL_RANK["reader"]:
            return "reader"
        return member_level
    # Private: only members get through. No member row → no access.
    return member_level


def _is_at_least(level: AccessLevel | None, required: AccessLevel) -> bool:
    return level is not None and _LEVEL_RANK[level] >= _LEVEL_RANK[required]


def visible_channels_stmt(user: User | None):
    """SQLAlchemy select for channels the caller is allowed to list.

    - Anon: public channels only.
    - Server admin: every channel.
    - Authenticated user: public OR channels they have an explicit
      membership row for.
    """
    stmt = select(Channel).order_by(Channel.name)
    if user is None:
        return stmt.where(Channel.private.is_(False))
    if user.role == "admin":
        return stmt
    member_channel_ids = select(ChannelMember.channel_id).where(ChannelMember.user_id == user.id)
    return stmt.where(or_(Channel.private.is_(False), Channel.id.in_(member_channel_ids)))


async def _get_channel_by_name(session: AsyncSession, name: str) -> Channel | None:
    result = await session.execute(select(Channel).where(Channel.name == name))
    return result.scalar_one_or_none()


async def visible_channel_or_404(session: AsyncSession, name: str, user: User | None) -> Channel:
    """Resolve a channel by name; 404 if it's missing or hidden from this caller.

    Returns 404 (not 403) for private-but-denied so the channel's existence
    isn't leaked to unauthorized callers.
    """
    channel = await _get_channel_by_name(session, name)
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="channel not found")
    level = await channel_access_level(session, channel, user)
    if level is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="channel not found")
    return channel


async def require_channel_writer(
    name: str,
    session: SessionDep,
    user: Annotated[User, Depends(current_user)],
) -> Channel:
    """Dep that resolves the channel and asserts caller has writer+ access.

    Returns the Channel so the handler can use it without a second fetch.
    404 for missing/hidden; 403 for visible-but-read-only.
    """
    channel = await _get_channel_by_name(session, name)
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="channel not found")
    level = await channel_access_level(session, channel, user)
    if level is None:
        # Reader wouldn't even see this channel; treat as missing.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="channel not found")
    if not _is_at_least(level, "writer"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Write access required",
        )
    return channel


async def require_channel_owner(
    name: str,
    session: SessionDep,
    user: Annotated[User, Depends(current_user)],
) -> Channel:
    """Dep that resolves the channel and asserts caller has owner+ access."""
    channel = await _get_channel_by_name(session, name)
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="channel not found")
    level = await channel_access_level(session, channel, user)
    if level is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="channel not found")
    if not _is_at_least(level, "owner"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Owner access required",
        )
    return channel


def _extract_bearer(request: Request) -> str | None:
    header = request.headers.get("Authorization")
    if not header or not header.lower().startswith("bearer "):
        return None
    return header[7:].strip() or None


def _has_session(request: Request) -> bool:
    return "session" in request.scope and bool(get_settings().auth.session_secret)
