"""Audit log: rows land for administrative actions + admin-only read API."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import select

from conda_server.db import get_sessionmaker
from conda_server.models import AuditLog, Channel, ChannelMember, User
from tests.conftest import make_session_cookie


async def _seed_admin() -> User:
    sm = get_sessionmaker()
    async with sm() as session:
        admin = User(subject="audit-admin", email="a@x", username="a", role="admin")
        session.add(admin)
        await session.commit()
        await session.refresh(admin)
        return admin


@pytest.mark.asyncio
async def test_create_channel_audits(app, client):
    admin = await _seed_admin()
    cookie = make_session_cookie(admin.subject)
    resp = await client.post(
        "/api/channels",
        json={"name": "auditch", "private": True},
        cookies={"session": cookie},
    )
    assert resp.status_code == 201

    sm = get_sessionmaker()
    async with sm() as session:
        rows = (
            (await session.execute(select(AuditLog).where(AuditLog.action == "channel.create")))
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].channel_name == "auditch"
        assert rows[0].actor_email == "a@x"


@pytest.mark.asyncio
async def test_member_lifecycle_audits(app, client):
    admin = await _seed_admin()
    cookie = make_session_cookie(admin.subject)

    sm = get_sessionmaker()
    async with sm() as session:
        target = User(subject="u1", email="u1@x", username="u1", role="user")
        channel = Channel(name="tc", storage_prefix="tc", private=True)
        session.add_all([target, channel])
        await session.flush()
        session.add(ChannelMember(channel_id=channel.id, user_id=admin.id, role="owner"))
        await session.commit()
        target_id = target.id

    # Add
    add = await client.post(
        "/api/channels/tc/members",
        json={"email": "u1@x", "role": "reader"},
        cookies={"session": cookie},
    )
    assert add.status_code == 201

    # Promote
    patched = await client.patch(
        f"/api/channels/tc/members/{target_id}",
        json={"role": "writer"},
        cookies={"session": cookie},
    )
    assert patched.status_code == 200

    # Remove
    removed = await client.delete(
        f"/api/channels/tc/members/{target_id}",
        cookies={"session": cookie},
    )
    assert removed.status_code == 204

    async with sm() as session:
        actions = [
            row.action
            for row in (await session.execute(select(AuditLog).order_by(AuditLog.id))).scalars()
            if row.action.startswith("member.")
        ]
    assert actions == ["member.add", "member.update", "member.remove"]


@pytest.mark.asyncio
async def test_list_audit_endpoint_admin_only(app, client):
    resp = await client.get("/api/admin/audit")
    assert resp.status_code == 401

    sm = get_sessionmaker()
    async with sm() as session:
        user = User(subject="plain", email="p@x", username="p", role="user")
        session.add(user)
        await session.commit()
    resp = await client.get(
        "/api/admin/audit",
        cookies={"session": make_session_cookie("plain")},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_list_audit_filters(app, client):
    admin = await _seed_admin()
    cookie = make_session_cookie(admin.subject)
    # Generate two entries: a create and a delete.
    await client.post(
        "/api/channels",
        json={"name": "a1"},
        cookies={"session": cookie},
    )
    await client.post(
        "/api/channels",
        json={"name": "a2"},
        cookies={"session": cookie},
    )
    await client.delete("/api/channels/a1", cookies={"session": cookie})

    all_rows = await client.get("/api/admin/audit", cookies={"session": cookie})
    assert all_rows.status_code == 200
    assert len(all_rows.json()) >= 3

    only_deletes = await client.get(
        "/api/admin/audit?action=channel.delete",
        cookies={"session": cookie},
    )
    assert all(r["action"] == "channel.delete" for r in only_deletes.json())
    assert only_deletes.json()[0]["channel_name"] == "a1"

    only_a2 = await client.get(
        "/api/admin/audit?channel=a2",
        cookies={"session": cookie},
    )
    assert all(r["channel_name"] == "a2" for r in only_a2.json())


@pytest.mark.asyncio
async def test_unknown_action_filter_returns_empty(app, client):
    admin = await _seed_admin()
    cookie = make_session_cookie(admin.subject)
    resp = await client.get(
        "/api/admin/audit?action=package.exploit",
        cookies={"session": cookie},
    )
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_upload_audit_row(app, client, tmp_path):
    from conda_server import storage as storage_module
    from conda_server.storage import LocalStore, ObstoreStorage

    admin = await _seed_admin()
    cookie = make_session_cookie(admin.subject)
    await client.post(
        "/api/channels",
        json={"name": "upa"},
        cookies={"session": cookie},
    )

    class _Idx:
        subdir = "linux-64"
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
            resp = await client.post(
                "/api/channels/upa/packages",
                files=[("files", ("x-1.0-0.conda", b"BYTES", "application/octet-stream"))],
                cookies={"session": cookie},
            )
        assert resp.status_code == 202
    finally:
        storage_module.reset_storage()

    sm = get_sessionmaker()
    async with sm() as session:
        rows = (
            (await session.execute(select(AuditLog).where(AuditLog.action == "package.upload")))
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].channel_name == "upa"
        assert rows[0].target == "x-1.0-0.conda"
        assert rows[0].meta["subdir"] == "linux-64"
