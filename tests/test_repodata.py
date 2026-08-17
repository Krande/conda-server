"""End-to-end tests for repodata + package download endpoints.

Validates that a LocalStore-backed storage (non-signing) falls back to streaming
bytes through the server when no presigned URL is available.
"""

from __future__ import annotations

import pytest
from obstore.store import MemoryStore

from conda_server import storage as storage_module
from conda_server.db import get_sessionmaker
from conda_server.models import Channel, Package, PackageVersion
from conda_server.storage import ObstoreStorage


@pytest.mark.asyncio
async def test_download_streams_when_signing_unsupported(app, client):
    mem = ObstoreStorage(MemoryStore(), supports_signing=False)
    storage_module.set_storage(mem)
    try:
        payload = b"fake-conda-package-bytes"
        await mem.put("mychan/linux-64/foo-1.0-0.conda", payload)

        sm = get_sessionmaker()
        async with sm() as session:
            channel = Channel(name="mychan", storage_prefix="mychan")
            session.add(channel)
            await session.flush()
            package = Package(channel_id=channel.id, name="foo")
            session.add(package)
            await session.flush()
            session.add(
                PackageVersion(
                    package_id=package.id,
                    version="1.0",
                    build="0",
                    build_number=0,
                    subdir="linux-64",
                    filename="foo-1.0-0.conda",
                    size=len(payload),
                )
            )
            await session.commit()

        response = await client.get("/mychan/linux-64/foo-1.0-0.conda")
        assert response.status_code == 200
        assert response.content == payload
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_download_404_when_missing(app, client):
    mem = ObstoreStorage(MemoryStore(), supports_signing=False)
    storage_module.set_storage(mem)
    try:
        sm = get_sessionmaker()
        async with sm() as session:
            session.add(Channel(name="empty", storage_prefix="empty"))
            await session.commit()

        response = await client.get("/empty/linux-64/nope-1.0-0.conda")
        assert response.status_code == 404
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_repodata_returns_valid_json(app, client):
    sm = get_sessionmaker()
    async with sm() as session:
        session.add(Channel(name="c", storage_prefix="c"))
        await session.commit()

    response = await client.get("/c/linux-64/repodata.json")
    assert response.status_code == 200
    body = response.json()
    assert body["info"]["subdir"] == "linux-64"
    assert body["repodata_version"] == 2
    assert body["packages"] == {}
    assert body["packages.conda"] == {}


@pytest.mark.asyncio
async def test_repodata_rejects_unknown_subdir(app, client):
    response = await client.get("/anything/not-a-subdir/repodata.json")
    assert response.status_code == 404
