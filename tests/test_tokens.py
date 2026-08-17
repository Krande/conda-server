"""API token lifecycle: mint, use, list, revoke, expire.

Uses the session-cookie helper to authenticate as a fixed user, then drives
the /api/auth/tokens endpoints like a real client would.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from conda_server.auth import hash_token
from conda_server.db import get_sessionmaker
from conda_server.models import ApiKey, User
from tests.conftest import make_session_cookie


async def _seed_user(subject: str = "u1", email: str = "u@x.com") -> User:
    sm = get_sessionmaker()
    async with sm() as session:
        user = User(subject=subject, email=email, username="u", role="user")
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


@pytest.mark.asyncio
async def test_mint_returns_token_once_and_lists_it(app, client):
    user = await _seed_user()
    cookie = make_session_cookie(user.subject)

    create = await client.post(
        "/api/auth/tokens",
        json={"description": "ci runner"},
        cookies={"session": cookie},
    )
    assert create.status_code == 201
    body = create.json()
    raw_token = body["token"]
    assert raw_token.startswith("cs_")
    assert body["description"] == "ci runner"
    assert body["expires_at"] is None

    listed = await client.get("/api/auth/tokens", cookies={"session": cookie})
    assert listed.status_code == 200
    items = listed.json()
    assert len(items) == 1
    assert items[0]["description"] == "ci runner"
    # The raw token must not leak in the list response.
    assert "token" not in items[0]


@pytest.mark.asyncio
async def test_minted_token_authenticates_bearer_request(app, client):
    user = await _seed_user()
    cookie = make_session_cookie(user.subject)

    create = await client.post("/api/auth/tokens", json={}, cookies={"session": cookie})
    raw = create.json()["token"]

    # Use the token (no session cookie) to call /me.
    me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {raw}"})
    assert me.status_code == 200
    assert me.json()["subject"] == user.subject


@pytest.mark.asyncio
async def test_revoke_invalidates_token(app, client):
    user = await _seed_user()
    cookie = make_session_cookie(user.subject)

    create = await client.post("/api/auth/tokens", json={}, cookies={"session": cookie})
    token_id = create.json()["id"]
    raw = create.json()["token"]

    revoke = await client.delete(f"/api/auth/tokens/{token_id}", cookies={"session": cookie})
    assert revoke.status_code == 204

    me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {raw}"})
    assert me.status_code == 401


@pytest.mark.asyncio
async def test_cannot_revoke_another_users_token(app, client):
    alice = await _seed_user(subject="alice", email="alice@x")
    bob = await _seed_user(subject="bob", email="bob@x")
    alice_cookie = make_session_cookie(alice.subject)
    bob_cookie = make_session_cookie(bob.subject)

    create = await client.post("/api/auth/tokens", json={}, cookies={"session": alice_cookie})
    token_id = create.json()["id"]

    # Bob must not be able to revoke Alice's token.
    revoke = await client.delete(f"/api/auth/tokens/{token_id}", cookies={"session": bob_cookie})
    assert revoke.status_code == 404


@pytest.mark.asyncio
async def test_expired_token_rejected(app, client):
    user = await _seed_user()

    # Insert an already-expired ApiKey directly.
    raw = "cs_expired_token_value"
    sm = get_sessionmaker()
    async with sm() as session:
        key = ApiKey(
            user_id=user.id,
            key_hash=hash_token(raw),
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        session.add(key)
        await session.commit()

    me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {raw}"})
    assert me.status_code == 401


@pytest.mark.asyncio
async def test_mint_with_expiry_sets_expires_at(app, client):
    user = await _seed_user()
    cookie = make_session_cookie(user.subject)

    create = await client.post(
        "/api/auth/tokens",
        json={"expires_in_days": 7},
        cookies={"session": cookie},
    )
    assert create.status_code == 201
    body = create.json()
    expires = datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00"))
    now = datetime.now(UTC)
    assert timedelta(days=6, hours=23) < (expires - now) < timedelta(days=7, hours=1)


@pytest.mark.asyncio
async def test_token_endpoints_require_auth(client):
    assert (await client.post("/api/auth/tokens", json={})).status_code == 401
    assert (await client.get("/api/auth/tokens")).status_code == 401
    assert (await client.delete("/api/auth/tokens/1")).status_code == 401
