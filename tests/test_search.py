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


async def _seed_late_republish() -> None:
    """The shape that broke: a package republished under a second subdir.

    A recipe moved from per-platform builds to ``noarch: python`` and a
    build ran on the merge commit before the version bump, so 2.4.0 exists
    both as a linux-64 artifact *and* as a noarch one — and the noarch
    republish landed **after** 2.4.1 had already shipped. 2.4.1 itself
    ships twice in the same subdir, ``__unix`` and ``__win`` builds of one
    noarch package (conda's standard pattern for platform-gated deps).

    So the newest row by ``created_at`` is 2.4.0, while the newest version
    is 2.4.1, and neither "one artifact per version" nor "one subdir per
    version" holds.
    """
    base = datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC)
    sm = get_sessionmaker()
    async with sm() as session:
        ch = Channel(name="example-channel", storage_prefix="example-channel", private=False)
        session.add(ch)
        await session.flush()
        pkg = Package(channel_id=ch.id, name="pkg-a")
        session.add(pkg)
        await session.flush()

        def _ver(version: str, build: str, subdir: str, minutes: int) -> PackageVersion:
            return PackageVersion(
                package_id=pkg.id,
                version=version,
                build=build,
                build_number=0,
                subdir=subdir,
                filename=f"pkg-a-{version}-{build}.conda",
                created_at=base + timedelta(minutes=minutes),
            )

        session.add_all(
            [
                _ver("2.4.0", "h3333333_0", "linux-64", 0),
                _ver("2.4.1", "pyh1111111_0", "noarch", 117),  # __unix
                _ver("2.4.1", "pyh2222222_0", "noarch", 117),  # __win
                _ver("2.4.0", "pyh1111111_0", "noarch", 119),  # late noarch republish
            ]
        )
        await session.commit()


@pytest.mark.asyncio
async def test_recent_shows_newest_version_not_newest_row(app, client):
    """A late republish of an older version must not become the shown version."""
    await _seed_late_republish()
    resp = await client.get("/api/search/recent")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["name"] == "pkg-a"
    # 2.4.0/noarch is the most recently created row; 2.4.1 is the newest
    # version. The panel must agree with the package page, which sorts by
    # conda version.
    assert rows[0]["version"] == "2.4.1"
    assert rows[0]["subdir"] == "noarch"
    # ...and report the timestamp of the version it names, not the later
    # upload of a different one.
    assert rows[0]["created_at"].startswith("2026-08-22T13:57")


@pytest.mark.asyncio
async def test_recent_agrees_with_package_endpoint(app, client):
    """The panel and the package page must not disagree about "latest"."""
    await _seed_late_republish()
    recent = (await client.get("/api/search/recent")).json()
    pkg = (await client.get("/api/channels/example-channel/packages/pkg-a")).json()
    assert recent[0]["version"] == pkg["versions"][0]["version"]


@pytest.mark.asyncio
async def test_recent_breaks_created_at_ties_by_version(app, client):
    """A batch import stamps every row identically — still pick 2.4.1.

    Without a deterministic tiebreak the answer was whatever order the DB
    returned rows in, which is how this bug stayed latent on one instance
    while reproducing on another with the same data.
    """
    stamp = datetime(2026, 8, 22, 16, 55, 35, tzinfo=UTC)
    sm = get_sessionmaker()
    async with sm() as session:
        ch = Channel(name="example-channel", storage_prefix="example-channel", private=False)
        session.add(ch)
        await session.flush()
        pkg = Package(channel_id=ch.id, name="pkg-a")
        session.add(pkg)
        await session.flush()
        session.add_all(
            [
                PackageVersion(
                    package_id=pkg.id,
                    version=version,
                    build=build,
                    build_number=0,
                    subdir=subdir,
                    filename=f"pkg-a-{version}-{build}.conda",
                    created_at=stamp,
                )
                for version, build, subdir in [
                    ("2.4.1", "pyh2222222_0", "noarch"),
                    ("2.4.0", "pyh1111111_0", "noarch"),
                    ("2.4.1", "pyh1111111_0", "noarch"),
                    ("2.4.0", "h3333333_0", "linux-64"),
                ]
            ]
        )
        await session.commit()

    rows = (await client.get("/api/search/recent")).json()
    assert [(r["name"], r["version"]) for r in rows] == [("pkg-a", "2.4.1")]


@pytest.mark.asyncio
async def test_recent_ranks_packages_by_upload_recency(app, client):
    """Ordering is still recency-based across packages, not version-based."""
    base = datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC)
    sm = get_sessionmaker()
    async with sm() as session:
        ch = Channel(name="example-channel", storage_prefix="example-channel", private=False)
        session.add(ch)
        await session.flush()
        old = Package(channel_id=ch.id, name="ancient")
        new = Package(channel_id=ch.id, name="fresh")
        session.add_all([old, new])
        await session.flush()
        session.add_all(
            [
                # Higher version number, but uploaded long ago.
                PackageVersion(
                    package_id=old.id,
                    version="9.0.0",
                    build="0",
                    build_number=0,
                    subdir="noarch",
                    filename="ancient-9.0.0-0.conda",
                    created_at=base,
                ),
                PackageVersion(
                    package_id=new.id,
                    version="0.1.0",
                    build="0",
                    build_number=0,
                    subdir="noarch",
                    filename="fresh-0.1.0-0.conda",
                    created_at=base + timedelta(hours=1),
                ),
            ]
        )
        await session.commit()

    rows = (await client.get("/api/search/recent")).json()
    assert [r["name"] for r in rows] == ["fresh", "ancient"]
