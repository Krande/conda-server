from __future__ import annotations

import asyncio

import typer
import uvicorn
from rich import print as rprint
from sqlalchemy import select

from conda_server import __version__
from conda_server.config import get_settings
from conda_server.db import dispose_engine, get_sessionmaker
from conda_server.indexer import reindex_channel
from conda_server.logging import configure_logging, get_logger
from conda_server.models import Channel, ChannelMember, User
from conda_server.storage import get_storage

app = typer.Typer(add_completion=False, help="conda-server CLI")
channel_app = typer.Typer(add_completion=False, help="Per-channel admin operations")
app.add_typer(channel_app, name="channel")
log = get_logger(__name__)


@app.command()
def version() -> None:
    """Show the installed version."""
    rprint(f"conda-server {__version__}")


@app.command()
def serve(
    host: str | None = typer.Option(None, help="Bind host (overrides config)"),
    port: int | None = typer.Option(None, help="Bind port (overrides config)"),
    reload: bool = typer.Option(False, "--reload", help="Enable auto-reload (dev only)"),
) -> None:
    """Run the API server under uvicorn."""
    settings = get_settings()
    configure_logging(settings.logging)
    uvicorn.run(
        "conda_server.app:create_app",
        factory=True,
        host=host or settings.host,
        port=port or settings.port,
        reload=reload,
        log_config=None,
    )


@app.command()
def reindex(
    channel_name: str = typer.Argument(..., help="Channel name to reindex"),
) -> None:
    """Reindex a channel from object storage."""
    asyncio.run(_reindex(channel_name))


async def _reindex(channel_name: str) -> None:
    settings = get_settings()
    configure_logging(settings.logging)
    storage = get_storage()
    sm = get_sessionmaker()
    try:
        async with sm() as session:
            result = await session.execute(select(Channel).where(Channel.name == channel_name))
            channel = result.scalar_one_or_none()
            if channel is None:
                rprint(f"[red]channel not found:[/red] {channel_name}")
                raise typer.Exit(code=1)
            outcome = await reindex_channel(session, storage, channel)
            await session.commit()
        rprint(
            f"[green]reindexed[/green] {outcome.channel}: "
            f"+{outcome.added} ~{outcome.updated} -{outcome.removed}"
        )
    finally:
        await dispose_engine()


@channel_app.command("grant")
def channel_grant(
    channel_name: str = typer.Argument(..., help="Channel name"),
    email: str = typer.Argument(..., help="Email of an existing user"),
    role: str = typer.Argument("writer", help="reader | writer | owner"),
) -> None:
    """Grant (or update) a user's role on a channel.

    Meant for bootstrapping — e.g. after a migration there's no owner yet,
    and a server admin can't log in as the target user to claim it. This
    bypasses HTTP and writes directly to the database.
    """
    if role not in ("reader", "writer", "owner"):
        rprint(f"[red]invalid role:[/red] {role}")
        raise typer.Exit(code=2)
    asyncio.run(_channel_grant(channel_name, email, role))


@channel_app.command("revoke")
def channel_revoke(
    channel_name: str = typer.Argument(..., help="Channel name"),
    email: str = typer.Argument(..., help="Email of the user to remove"),
) -> None:
    """Remove a user from a channel's ACL."""
    asyncio.run(_channel_revoke(channel_name, email))


@channel_app.command("members")
def channel_members(
    channel_name: str = typer.Argument(..., help="Channel name"),
) -> None:
    """List the ACL for a channel."""
    asyncio.run(_channel_members(channel_name))


async def _channel_grant(channel_name: str, email: str, role: str) -> None:
    settings = get_settings()
    configure_logging(settings.logging)
    sm = get_sessionmaker()
    try:
        async with sm() as session:
            channel = await _resolve_channel(session, channel_name)
            user = await _resolve_user(session, email)
            existing = await session.execute(
                select(ChannelMember).where(
                    ChannelMember.channel_id == channel.id,
                    ChannelMember.user_id == user.id,
                )
            )
            member = existing.scalar_one_or_none()
            if member is None:
                session.add(ChannelMember(channel_id=channel.id, user_id=user.id, role=role))
                await session.commit()
                rprint(f"[green]granted[/green] {email} {role!r} on {channel_name}")
            else:
                member.role = role
                await session.commit()
                rprint(f"[green]updated[/green] {email} → {role!r} on {channel_name}")
    finally:
        await dispose_engine()


async def _channel_revoke(channel_name: str, email: str) -> None:
    settings = get_settings()
    configure_logging(settings.logging)
    sm = get_sessionmaker()
    try:
        async with sm() as session:
            channel = await _resolve_channel(session, channel_name)
            user = await _resolve_user(session, email)
            result = await session.execute(
                select(ChannelMember).where(
                    ChannelMember.channel_id == channel.id,
                    ChannelMember.user_id == user.id,
                )
            )
            member = result.scalar_one_or_none()
            if member is None:
                rprint(f"[yellow]{email}[/yellow] is not a member of {channel_name}")
                return
            await session.delete(member)
            await session.commit()
            rprint(f"[green]revoked[/green] {email} from {channel_name}")
    finally:
        await dispose_engine()


async def _channel_members(channel_name: str) -> None:
    settings = get_settings()
    configure_logging(settings.logging)
    sm = get_sessionmaker()
    try:
        async with sm() as session:
            channel = await _resolve_channel(session, channel_name)
            result = await session.execute(
                select(ChannelMember, User)
                .join(User, User.id == ChannelMember.user_id)
                .where(ChannelMember.channel_id == channel.id)
                .order_by(User.email)
            )
            rows = list(result.all())
            if not rows:
                rprint(f"[yellow]{channel_name}[/yellow] has no members")
                return
            for member, user in rows:
                rprint(f"  {member.role:<6}  {user.email or '(no email)'}")
    finally:
        await dispose_engine()


async def _resolve_channel(session, name: str) -> Channel:
    result = await session.execute(select(Channel).where(Channel.name == name))
    channel = result.scalar_one_or_none()
    if channel is None:
        rprint(f"[red]channel not found:[/red] {name}")
        raise typer.Exit(code=1)
    return channel


async def _resolve_user(session, email: str) -> User:
    result = await session.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user is None:
        rprint(f"[red]no user with email[/red] {email} — they need to log in at least once first")
        raise typer.Exit(code=1)
    return user


if __name__ == "__main__":
    app()
