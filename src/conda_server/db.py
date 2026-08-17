from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from conda_server.config import DatabaseSettings, get_settings


class Base(DeclarativeBase):
    pass


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _build_engine(settings: DatabaseSettings) -> AsyncEngine:
    kwargs: dict[str, object] = {"echo": settings.echo}
    if settings.url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs["pool_size"] = settings.pool_size
        kwargs["max_overflow"] = settings.max_overflow
    return create_async_engine(settings.url, **kwargs)


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = _build_engine(get_settings().database)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]
