"""Per-channel ACL: reader/writer/owner roles + server-admin bypass.

Private channels hide from non-members; public channels are readable by
everyone but write operations still require membership. The last owner
of a channel can't be removed via the API (promote someone else first).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from conda_server.db import get_sessionmaker
from conda_server.models import Channel, ChannelMember, User
from tests.conftest import make_session_cookie


async def _add_user(subject: str, email: str, role: str = "user") -> User:
    sm = get_sessionmaker()
    async with sm() as session:
        user = User(subject=subject, email=email, username=subject, role=role)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def _add_channel(name: str, private: bool = True, mirror_url: str | None = None) -> Channel:
    sm = get_sessionmaker()
    async with sm() as session:
        channel = Channel(
            name=name,
            storage_prefix=name,
            private=private,
            mirror_url=mirror_url,
        )
        session.add(channel)
        await session.commit()
        await session.refresh(channel)
        return channel


async def _grant(channel: Channel, user: User, role: str) -> None:
    sm = get_sessionmaker()
    async with sm() as session:
        session.add(ChannelMember(channel_id=channel.id, user_id=user.id, role=role))
        await session.commit()


@pytest.mark.asyncio
async def test_private_channel_hidden_from_non_member(app, client):
    user = await _add_user("outsider", "out@x")
    await _add_channel("hush")
    cookie = make_session_cookie(user.subject)

    # Not visible in list.
    listing = await client.get("/api/channels", cookies={"session": cookie})
    assert [c["name"] for c in listing.json()] == []

    # Detail 404s.
    detail = await client.get("/api/channels/hush", cookies={"session": cookie})
    assert detail.status_code == 404


@pytest.mark.asyncio
async def test_reader_sees_private_channel_but_cant_write(app, client, tmp_path):
    from conda_server import storage as storage_module
    from conda_server.storage import LocalStore, ObstoreStorage

    reader = await _add_user("reader1", "reader@x")
    channel = await _add_channel("team-a")
    await _grant(channel, reader, "reader")
    cookie = make_session_cookie(reader.subject)

    detail = await client.get("/api/channels/team-a", cookies={"session": cookie})
    assert detail.status_code == 200
    assert detail.json()["my_role"] == "reader"

    # Reader can't upload.
    storage_module.set_storage(ObstoreStorage(LocalStore(str(tmp_path)), supports_signing=False))
    try:
        up = await client.post(
            "/api/channels/team-a/packages",
            files=[("files", ("x-1.0-0.conda", b"BYTES", "application/octet-stream"))],
            cookies={"session": cookie},
        )
        assert up.status_code == 403
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_writer_can_upload(app, client, tmp_path):
    from conda_server import storage as storage_module
    from conda_server.storage import LocalStore, ObstoreStorage

    writer = await _add_user("writer1", "w@x")
    channel = await _add_channel("team-b")
    await _grant(channel, writer, "writer")
    cookie = make_session_cookie(writer.subject)

    class _Idx:
        subdir = "noarch"
        name = "x"
        version = "1.0"
        build = "0"

    storage_module.set_storage(ObstoreStorage(LocalStore(str(tmp_path)), supports_signing=False))
    try:
        with (
            patch(
                "conda_server.api.channels.rattler.IndexJson.from_package_archive",
                return_value=_Idx(),
            ),
            patch("conda_server.api.channels._reindex_background"),
        ):
            up = await client.post(
                "/api/channels/team-b/packages",
                files=[("files", ("x-1.0-0.conda", b"BYTES", "application/octet-stream"))],
                cookies={"session": cookie},
            )
        assert up.status_code == 202
        assert up.json()["results"][0]["status"] == "stored"
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_writer_cannot_manage_members(app, client):
    writer = await _add_user("writer2", "w2@x")
    other = await _add_user("other", "o@x")
    channel = await _add_channel("team-c")
    await _grant(channel, writer, "writer")
    cookie = make_session_cookie(writer.subject)

    # Add/remove member requires owner+.
    resp = await client.post(
        "/api/channels/team-c/members",
        json={"email": other.email, "role": "reader"},
        cookies={"session": cookie},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_owner_manages_members(app, client):
    owner = await _add_user("owner1", "o1@x")
    target = await _add_user("t1", "t1@x")
    channel = await _add_channel("team-d")
    await _grant(channel, owner, "owner")
    cookie = make_session_cookie(owner.subject)

    # Add
    resp = await client.post(
        "/api/channels/team-d/members",
        json={"email": target.email, "role": "reader"},
        cookies={"session": cookie},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["role"] == "reader"

    # List
    listing = await client.get("/api/channels/team-d/members", cookies={"session": cookie})
    emails = [m["email"] for m in listing.json()]
    assert set(emails) == {owner.email, target.email}

    # Patch to writer
    patched = await client.patch(
        f"/api/channels/team-d/members/{target.id}",
        json={"role": "writer"},
        cookies={"session": cookie},
    )
    assert patched.status_code == 200
    assert patched.json()["role"] == "writer"

    # Remove
    removed = await client.delete(
        f"/api/channels/team-d/members/{target.id}",
        cookies={"session": cookie},
    )
    assert removed.status_code == 204


@pytest.mark.asyncio
async def test_cannot_remove_last_owner(app, client):
    owner = await _add_user("only-owner", "oo@x")
    channel = await _add_channel("team-e")
    await _grant(channel, owner, "owner")
    cookie = make_session_cookie(owner.subject)

    # Demote attempt: deny.
    demote = await client.patch(
        f"/api/channels/team-e/members/{owner.id}",
        json={"role": "writer"},
        cookies={"session": cookie},
    )
    assert demote.status_code == 409

    # Delete attempt: deny.
    delete = await client.delete(
        f"/api/channels/team-e/members/{owner.id}",
        cookies={"session": cookie},
    )
    assert delete.status_code == 409


@pytest.mark.asyncio
async def test_owner_can_delete_channel(app, client):
    owner = await _add_user("delowner", "d@x")
    channel = await _add_channel("team-f")
    await _grant(channel, owner, "owner")
    cookie = make_session_cookie(owner.subject)

    resp = await client.delete("/api/channels/team-f", cookies={"session": cookie})
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_server_admin_bypasses_acl(app, client):
    server_admin = await _add_user("sa", "sa@x", role="admin")
    await _add_channel("team-g")
    cookie = make_session_cookie(server_admin.subject)

    # No membership row, yet admin can see + manage.
    detail = await client.get("/api/channels/team-g", cookies={"session": cookie})
    assert detail.status_code == 200
    assert detail.json()["my_role"] == "admin"

    # And can delete.
    resp = await client.delete("/api/channels/team-g", cookies={"session": cookie})
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_public_channel_reader_for_all(app, client):
    # Anonymous: sees public, my_role=reader.
    await _add_channel("pub", private=False)

    listing = await client.get("/api/channels")
    assert listing.status_code == 200
    rows = {c["name"]: c for c in listing.json()}
    assert "pub" in rows
    assert rows["pub"]["my_role"] == "reader"

    # Writes still require auth (401) not ACL (403).
    up = await client.post(
        "/api/channels/pub/packages",
        files=[("files", ("x-1.0-0.conda", b"", "application/octet-stream"))],
    )
    assert up.status_code == 401


@pytest.mark.asyncio
async def test_create_channel_auto_enrolls_admin_as_owner(app, client):
    server_admin = await _add_user("sa2", "sa2@x", role="admin")
    cookie = make_session_cookie(server_admin.subject)

    resp = await client.post(
        "/api/channels",
        json={"name": "fresh", "private": True},
        cookies={"session": cookie},
    )
    assert resp.status_code == 201, resp.text

    # Verify the ChannelMember row landed.
    sm = get_sessionmaker()
    from sqlalchemy import select

    async with sm() as session:
        chan = (await session.execute(select(Channel).where(Channel.name == "fresh"))).scalar_one()
        members = (
            (
                await session.execute(
                    select(ChannelMember).where(ChannelMember.channel_id == chan.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(members) == 1
        assert members[0].user_id == server_admin.id
        assert members[0].role == "owner"


@pytest.mark.asyncio
async def test_add_member_rejects_unknown_email(app, client):
    owner = await _add_user("o2", "o2@x")
    channel = await _add_channel("team-h")
    await _grant(channel, owner, "owner")
    cookie = make_session_cookie(owner.subject)

    resp = await client.post(
        "/api/channels/team-h/members",
        json={"email": "nobody@x", "role": "reader"},
        cookies={"session": cookie},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_add_member_duplicate_conflict(app, client):
    owner = await _add_user("o3", "o3@x")
    target = await _add_user("t3", "t3@x")
    channel = await _add_channel("team-i")
    await _grant(channel, owner, "owner")
    await _grant(channel, target, "reader")
    cookie = make_session_cookie(owner.subject)

    resp = await client.post(
        "/api/channels/team-i/members",
        json={"email": target.email, "role": "writer"},
        cookies={"session": cookie},
    )
    assert resp.status_code == 409
