"""Auth tests — OIDC upsert logic, session round-trips, admin bootstrap.

Actual OIDC network flow is mocked at the ``authorize_access_token`` boundary
so tests run offline with no IdP.
"""

from __future__ import annotations

import pytest

from conda_server.api import auth as auth_api
from conda_server.config import get_settings, reset_settings
from conda_server.db import get_sessionmaker
from conda_server.models import User


@pytest.mark.asyncio
async def test_me_requires_auth(client):
    response = await client.get("/api/auth/me")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_logout_clears_session(client):
    response = await client.post("/api/auth/logout")
    assert response.status_code == 200
    assert response.json() == {"status": "logged_out"}


@pytest.mark.asyncio
async def test_login_returns_503_when_oidc_unconfigured(client):
    # Default test settings don't configure OIDC.
    response = await client.get("/api/auth/login", follow_redirects=False)
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_upsert_creates_new_user(app):
    sm = get_sessionmaker()
    async with sm() as session:
        user = await auth_api.upsert_user_from_userinfo(
            session,
            {
                "sub": "authentik|abc123",
                "email": "alice@example.com",
                "preferred_username": "alice",
                "name": "Alice",
            },
        )
        await session.commit()

    assert user.subject == "authentik|abc123"
    assert user.email == "alice@example.com"
    assert user.username == "alice"
    assert user.role == "user"


@pytest.mark.asyncio
async def test_upsert_promotes_admin_from_initial_admins(app, monkeypatch):
    monkeypatch.setenv("CONDA_SERVER_AUTH__INITIAL_ADMINS", '["admin@example.com"]')
    reset_settings()
    assert "admin@example.com" in get_settings().auth.initial_admins

    sm = get_sessionmaker()
    async with sm() as session:
        user = await auth_api.upsert_user_from_userinfo(
            session,
            {
                "sub": "authentik|root",
                "email": "Admin@Example.com",  # case-insensitive match
                "preferred_username": "root",
            },
        )
        await session.commit()

    assert user.role == "admin"


@pytest.mark.asyncio
async def test_upsert_updates_existing_user_fields(app):
    sm = get_sessionmaker()
    async with sm() as session:
        first = await auth_api.upsert_user_from_userinfo(
            session,
            {"sub": "s1", "email": "old@x.com", "preferred_username": "bob"},
        )
        await session.commit()
        original_id = first.id

        second = await auth_api.upsert_user_from_userinfo(
            session,
            {"sub": "s1", "email": "new@x.com", "preferred_username": "robert"},
        )
        await session.commit()

        assert second.id == original_id
        assert second.email == "new@x.com"
        assert second.username == "robert"


@pytest.mark.asyncio
async def test_upsert_rejects_missing_sub(app):
    sm = get_sessionmaker()
    async with sm() as session:
        with pytest.raises(Exception) as exc_info:
            await auth_api.upsert_user_from_userinfo(session, {"email": "x@y.z"})
        assert "sub" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_session_cookie_identifies_user(app, client):
    # Create a user directly, then set the session cookie as the callback would.
    sm = get_sessionmaker()
    async with sm() as session:
        user = User(subject="authentik|zoe", email="zoe@x.com", username="zoe", role="user")
        session.add(user)
        await session.commit()

    import base64
    import json

    from itsdangerous import TimestampSigner

    secret = get_settings().auth.session_secret
    signer = TimestampSigner(secret)
    session_data = base64.b64encode(json.dumps({"sub": "authentik|zoe"}).encode()).decode()
    cookie = signer.sign(session_data).decode()

    response = await client.get("/api/auth/me", cookies={"session": cookie})
    assert response.status_code == 200
    body = response.json()
    assert body["subject"] == "authentik|zoe"
    assert body["email"] == "zoe@x.com"


@pytest.mark.asyncio
async def test_callback_creates_user_and_sets_session(app, client, monkeypatch):
    """Drive the callback route with a mocked authorize_access_token."""
    monkeypatch.setenv(
        "CONDA_SERVER_AUTH__OIDC__ISSUER", "https://authentik.test/application/o/cs/"
    )
    monkeypatch.setenv("CONDA_SERVER_AUTH__OIDC__CLIENT_ID", "fake-id")
    monkeypatch.setenv("CONDA_SERVER_AUTH__OIDC__CLIENT_SECRET", "fake-secret")
    reset_settings()
    auth_api.reset_oauth()

    class FakeClient:
        async def authorize_access_token(self, request):
            return {
                "userinfo": {
                    "sub": "authentik|new-user",
                    "email": "new@example.com",
                    "preferred_username": "new",
                }
            }

    class FakeOAuth:
        def create_client(self, name):
            return FakeClient()

    monkeypatch.setattr(auth_api, "get_oauth", lambda: FakeOAuth())

    response = await client.get("/api/auth/callback", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/"

    # Row exists
    sm = get_sessionmaker()
    async with sm() as session:
        from sqlalchemy import select

        result = await session.execute(select(User).where(User.subject == "authentik|new-user"))
        assert result.scalar_one_or_none() is not None

    # Cookie-driven /me works
    cookies = response.cookies
    me = await client.get("/api/auth/me", cookies=cookies)
    assert me.status_code == 200
    assert me.json()["subject"] == "authentik|new-user"
