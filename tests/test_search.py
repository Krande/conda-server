"""Cross-channel search respects ACL + minimum query length."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from conda_server.db import get_sessionmaker
from conda_server.models import (
    Channel,
    ChannelMember,
    Package,
    PackageVersion,
    User,
)
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


async def _seed_recent() -> dict[str, int]:
    """Public + private channels each with versioned packages at known
    upload times, plus a mirror channel that shouldn't surface.

    Upload times (created_at) descend so ordering is deterministic:
    pub/alpha 2.0 (newest) > pub/alpha 1.0 > pub/beta 1.0 >
    secret/gamma 1.0 (oldest).
    """
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    sm = get_sessionmaker()
    async with sm() as session:
        pub = Channel(name="pub", storage_prefix="pub", private=False)
        priv = Channel(name="secret", storage_prefix="secret", private=True)
        mir = Channel(
            name="cf-mirror",
            storage_prefix="cf-mirror",
            private=False,
            mirror_url="https://conda.anaconda.org/conda-forge",
        )
        session.add_all([pub, priv, mir])
        await session.flush()

        alpha = Package(channel_id=pub.id, name="alpha")
        beta = Package(channel_id=pub.id, name="beta")
        gamma = Package(channel_id=priv.id, name="gamma")
        # A mirror channel with a Package row + version — must be excluded.
        delta = Package(channel_id=mir.id, name="delta")
        session.add_all([alpha, beta, gamma, delta])
        await session.flush()

        def _ver(pkg_id: int, version: str, minutes_ago: int) -> PackageVersion:
            return PackageVersion(
                package_id=pkg_id,
                version=version,
                build="0",
                build_number=0,
                subdir="noarch",
                filename=f"{version}-0.conda",
                created_at=base - timedelta(minutes=minutes_ago),
            )

        session.add_all(
            [
                _ver(alpha.id, "2.0", 1),  # newest
                _ver(alpha.id, "1.0", 5),  # same package, older
                _ver(beta.id, "1.0", 10),
                _ver(gamma.id, "1.0", 20),  # oldest, private
                _ver(delta.id, "1.0", 0),  # mirror — newest of all, excluded
            ]
        )
        await session.commit()
        return {"pub": pub.id, "priv": priv.id, "mir": mir.id}


@pytest.mark.asyncio
async def test_recent_anon_sees_public_newest_first_deduped(app, client):
    await _seed_recent()
    resp = await client.get("/api/search/recent")
    assert resp.status_code == 200
    rows = resp.json()
    # Mirror (delta) excluded; private (gamma) hidden from anon; alpha
    # deduped to its newest version (2.0). Newest-first ordering.
    assert [(r["name"], r["channel"], r["version"]) for r in rows] == [
        ("alpha", "pub", "2.0"),
        ("beta", "pub", "1.0"),
    ]


@pytest.mark.asyncio
async def test_recent_member_sees_private(app, client):
    ids = await _seed_recent()
    sm = get_sessionmaker()
    async with sm() as session:
        user = User(subject="rm", email="rm@x", username="rm", role="user")
        session.add(user)
        await session.flush()
        session.add(ChannelMember(channel_id=ids["priv"], user_id=user.id, role="reader"))
        await session.commit()

    resp = await client.get(
        "/api/search/recent",
        cookies={"session": make_session_cookie("rm")},
    )
    names = [(r["name"], r["channel"]) for r in resp.json()]
    assert ("gamma", "secret") in names
    assert ("delta", "cf-mirror") not in names  # mirror still excluded


@pytest.mark.asyncio
async def test_recent_respects_limit(app, client):
    await _seed_recent()
    resp = await client.get("/api/search/recent?limit=1")
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["name"] == "alpha"  # the single newest distinct package


@pytest.mark.asyncio
async def test_recent_empty_when_no_uploads(app, client):
    await _seed_world()  # packages but no PackageVersion rows
    resp = await client.get("/api/search/recent")
    assert resp.status_code == 200
    assert resp.json() == []


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
