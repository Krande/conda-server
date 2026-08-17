from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault("CONDA_SERVER_DATABASE__URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("CONDA_SERVER_STORAGE__BACKEND", "local")
os.environ.setdefault("CONDA_SERVER_STORAGE__URL", "./data-test")
os.environ.setdefault("CONDA_SERVER_AUTH__SESSION_SECRET", "test-secret")
os.environ.setdefault("CONDA_SERVER_AUTH__SESSION_HTTPS_ONLY", "false")


@pytest_asyncio.fixture
async def app():
    from conda_server import db as db_module
    from conda_server.app import create_app
    from conda_server.config import reset_settings
    from conda_server.db import Base
    from conda_server.storage import reset_storage

    reset_settings()
    reset_storage()
    # Each test gets a fresh in-memory SQLite engine so schema state doesn't leak.
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    db_module._engine = engine
    db_module._sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    app = create_app()
    try:
        yield app
    finally:
        await engine.dispose()
        db_module._engine = None
        db_module._sessionmaker = None


@pytest_asyncio.fixture
async def client(app) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def _silence_warnings():
    import warnings

    warnings.filterwarnings("ignore", category=DeprecationWarning)


def make_session_cookie(subject: str) -> str:
    """Build a Starlette-compatible session cookie for the given OIDC subject.

    Mirrors SessionMiddleware's encoding (base64 JSON signed with itsdangerous)
    so tests can act as a logged-in user without driving the OIDC flow.
    """
    import base64
    import json

    from itsdangerous import TimestampSigner

    from conda_server.config import get_settings

    signer = TimestampSigner(get_settings().auth.session_secret)
    payload = base64.b64encode(json.dumps({"sub": subject}).encode()).decode()
    return signer.sign(payload).decode()
