"""Cross-channel search respects ACL + minimum query length."""

from __future__ import annotations

import pytest

from conda_server.db import get_sessionmaker
from conda_server.models import Channel, ChannelMember, Package, User
from tests.conftest import make_session_cookie


async def _seed_world() -> dict[str, int]:
    """Build: one public channel, one private, and a few packages each."""
    sm = get_sessionmaker()
    async with sm() as session:
        pub = Channel(name="pub", storage_prefix="pub", private=False)
        priv = Channel(name="secret", storage_prefix="secret", private=True)
        session.add_all([pub, priv])
        await session.flush()
        session.add_all(
            [
                Package(channel_id=pub.id, name="numpy"),
                Package(channel_id=pub.id, name="numpy-quaternion"),
                Package(channel_id=priv.id, name="numpy-internal"),
                Package(channel_id=priv.id, name="private-util"),
            ]
        )
        await session.commit()
        return {"pub": pub.id, "priv": priv.id}


@pytest.mark.asyncio
async def test_anon_sees_only_public_hits(app, client):
    await _seed_world()
    resp = await client.get("/api/search?q=numpy")
    assert resp.status_code == 200
    body = resp.json()
    names = sorted((r["name"], r["channel"]) for r in body["packages"])
    assert names == [("numpy", "pub"), ("numpy-quaternion", "pub")]


@pytest.mark.asyncio
async def test_member_sees_own_private_hits(app, client):
    ids = await _seed_world()
    sm = get_sessionmaker()
    async with sm() as session:
        user = User(subject="m1", email="m1@x", username="m1", role="user")
        session.add(user)
        await session.flush()
        session.add(ChannelMember(channel_id=ids["priv"], user_id=user.id, role="reader"))
        await session.commit()
        subj = user.subject

    resp = await client.get(
        "/api/search?q=numpy",
        cookies={"session": make_session_cookie(subj)},
    )
    assert resp.status_code == 200
    names = sorted(r["name"] for r in resp.json()["packages"])
    # Both public hits + the private member-visible one.
    assert names == ["numpy", "numpy-internal", "numpy-quaternion"]


@pytest.mark.asyncio
async def test_short_query_returns_empty(app, client):
    await _seed_world()
    resp = await client.get("/api/search?q=n")
    assert resp.status_code == 200
    assert resp.json() == {"packages": [], "channels": []}


@pytest.mark.asyncio
async def test_channel_name_match(app, client):
    await _seed_world()
    resp = await client.get("/api/search?q=secr")
    # Anon doesn't see the private channel.
    assert [c["name"] for c in resp.json()["channels"]] == []


@pytest.mark.asyncio
async def test_admin_sees_everything(app, client):
    await _seed_world()
    sm = get_sessionmaker()
    async with sm() as session:
        admin = User(subject="adm", email="adm@x", username="adm", role="admin")
        session.add(admin)
        await session.commit()
        subj = admin.subject

    resp = await client.get(
        "/api/search?q=numpy",
        cookies={"session": make_session_cookie(subj)},
    )
    assert resp.status_code == 200
    names = sorted(r["name"] for r in resp.json()["packages"])
    assert names == ["numpy", "numpy-internal", "numpy-quaternion"]


@pytest.mark.asyncio
async def test_case_insensitive_match(app, client):
    await _seed_world()
    resp = await client.get("/api/search?q=NUMPY")
    names = [r["name"] for r in resp.json()["packages"]]
    assert "numpy" in names


@pytest.mark.asyncio
async def test_resolve_finds_packages_in_non_mirror_channels(app, client):
    ids = await _seed_world()
    _ = ids
    # Extra: add a mirror channel with a Package row (shouldn't resolve
    # from mirrors, even if the row happens to exist in the DB).
    sm = get_sessionmaker()
    async with sm() as session:
        cf = Channel(
            name="cf-mirror",
            storage_prefix="cf-mirror",
            private=False,
            mirror_url="https://conda.anaconda.org/conda-forge",
        )
        session.add(cf)
        await session.flush()
        session.add(Package(channel_id=cf.id, name="numpy"))
        await session.commit()

    resp = await client.get("/api/search/resolve?names=numpy,scipy,nonexistent")
    assert resp.status_code == 200
    data = resp.json()
    # numpy resolves to the public non-mirror channel (pub), NOT the mirror.
    assert data["numpy"] == {"channel": "pub"}
    # scipy isn't in any seeded channel.
    assert "scipy" not in data
    # nonexistent obviously isn't.
    assert "nonexistent" not in data


@pytest.mark.asyncio
async def test_resolve_respects_acl(app, client):
    ids = await _seed_world()
    # Private channel has "numpy-internal"; anon can't see it.
    resp = await client.get("/api/search/resolve?names=numpy-internal")
    assert resp.json() == {}

    # Member of the private channel can.
    sm = get_sessionmaker()
    async with sm() as session:
        user = User(subject="m-resolve", email="m@r", username="m", role="user")
        session.add(user)
        await session.flush()
        session.add(
            ChannelMember(
                channel_id=ids["priv"],
                user_id=user.id,
                role="reader",
            )
        )
        await session.commit()

    resp = await client.get(
        "/api/search/resolve?names=numpy-internal",
        cookies={"session": make_session_cookie("m-resolve")},
    )
    assert resp.json() == {"numpy-internal": {"channel": "secret"}}


@pytest.mark.asyncio
async def test_resolve_caps_input_length(app, client):
    await _seed_world()
    # 150 names, only one of which ("numpy") resolves. Server should cap
    # to 100 but we picked numpy as the first so it still resolves.
    names = ["numpy"] + [f"bogus-{i}" for i in range(149)]
    resp = await client.get(f"/api/search/resolve?names={','.join(names)}")
    assert resp.status_code == 200
    assert resp.json().get("numpy") == {"channel": "pub"}


@pytest.mark.asyncio
async def test_limit_caps_results(app, client):
    sm = get_sessionmaker()
    async with sm() as session:
        ch = Channel(name="many", storage_prefix="many", private=False)
        session.add(ch)
        await session.flush()
        for i in range(30):
            session.add(Package(channel_id=ch.id, name=f"foo-{i:02d}"))
        await session.commit()

    resp = await client.get("/api/search?q=foo&limit=5")
    assert len(resp.json()["packages"]) == 5
