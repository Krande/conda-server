"""Append-only audit log.

Centralized here so call sites get a one-liner and we can evolve the
payload shape without touching twelve files. The helper takes the same
session that's already open for the request — the audit row lands in
the same transaction as the action it describes, so a rolled-back
request doesn't leave a misleading audit entry.

Actions follow a ``<noun>.<verb>`` convention: ``channel.create``,
``package.upload``, ``member.add``, etc. Keep the set small; UI filters
hard-code it.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from conda_server.models import AuditLog, User

# Canonical actions. Listed here so a typo in a call site fails fast
# when tests diff against this set.
ACTIONS = frozenset(
    {
        "channel.create",
        "channel.delete",
        "channel.reindex",
        "package.upload",
        "package.delete",
        "package.import",
        "member.add",
        "member.update",
        "member.remove",
    }
)


async def record(
    session: AsyncSession,
    actor: User | None,
    action: str,
    *,
    channel_name: str | None = None,
    target: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """Append a row. Caller is expected to commit the enclosing session."""
    assert action in ACTIONS, f"unknown audit action: {action!r}"
    session.add(
        AuditLog(
            actor_id=actor.id if actor is not None else None,
            actor_email=actor.email if actor is not None else None,
            action=action,
            channel_name=channel_name,
            target=target,
            meta=meta or {},
        )
    )
