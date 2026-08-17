"""Mirror-channel package list is derived from (cached repodata ∩ stored bytes).

Seeds a cached repodata.json plus a single cached .conda file, then verifies
the list and detail endpoints only report that one package.
"""

from __future__ import annotations

import json

import pytest

from conda_server import storage as storage_module
from conda_server.db import get_sessionmaker
from conda_server.models import Channel
from conda_server.storage import LocalStore, ObstoreStorage

REPODATA = {
    "info": {"subdir": "linux-64"},
    "packages": {},
    "packages.conda": {
        "xtensor-0.25.0-hf036a51_0.conda": {
            "name": "xtensor",
            "version": "0.25.0",
            "build": "hf036a51_0",
            "build_number": 0,
            "size": 123456,
            "sha256": "a" * 64,
        },
        # Present in upstream but NOT cached locally — should be excluded.
        "numpy-1.26.0-py312h1_0.conda": {
            "name": "numpy",
            "version": "1.26.0",
            "build": "py312h1_0",
            "build_number": 0,
            "size": 7654321,
            "sha256": "b" * 64,
        },
    },
    "repodata_version": 2,
}


async def _seed_mirror_channel(name: str = "cf") -> Channel:
    sm = get_sessionmaker()
    async with sm() as session:
        channel = Channel(
            name=name,
            storage_prefix=name,
            mirror_url="https://upstream.example",
            mirror_cache_seconds=900,
        )
        session.add(channel)
        await session.commit()
        await session.refresh(channel)
        return channel


@pytest.fixture
def local_storage(tmp_path):
    store = ObstoreStorage(LocalStore(str(tmp_path)), supports_signing=False)
    storage_module.set_storage(store)
    yield store, tmp_path
    storage_module.reset_storage()


@pytest.mark.asyncio
async def test_mirror_list_only_returns_cached_packages(app, client, local_storage):
    storage, _ = local_storage
    channel = await _seed_mirror_channel()

    # Seed: cached repodata mentions two packages, only one package file is
    # actually present in storage.
    await storage.put(
        f"{channel.storage_prefix}/linux-64/repodata.json",
        json.dumps(REPODATA).encode(),
    )
    await storage.put(
        f"{channel.storage_prefix}/linux-64/xtensor-0.25.0-hf036a51_0.conda",
        b"FAKE",
    )

    resp = await client.get(f"/api/channels/{channel.name}/packages")
    assert resp.status_code == 200
    body = resp.json()
    names = sorted(p["name"] for p in body)
    assert names == ["xtensor"]
    versions = body[0]["versions"]
    assert len(versions) == 1
    assert versions[0]["filename"] == "xtensor-0.25.0-hf036a51_0.conda"
    assert versions[0]["subdir"] == "linux-64"


@pytest.mark.asyncio
async def test_mirror_detail_returns_cached_package(app, client, local_storage):
    storage, _ = local_storage
    channel = await _seed_mirror_channel()
    await storage.put(
        f"{channel.storage_prefix}/linux-64/repodata.json",
        json.dumps(REPODATA).encode(),
    )
    await storage.put(
        f"{channel.storage_prefix}/linux-64/xtensor-0.25.0-hf036a51_0.conda",
        b"FAKE",
    )

    resp = await client.get(f"/api/channels/{channel.name}/packages/xtensor")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "xtensor"
    assert body["versions"][0]["version"] == "0.25.0"

    # numpy is in repodata but not cached — detail 404s.
    missing = await client.get(f"/api/channels/{channel.name}/packages/numpy")
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_mirror_list_empty_when_nothing_cached(app, client, local_storage):
    channel = await _seed_mirror_channel()

    resp = await client.get(f"/api/channels/{channel.name}/packages")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_non_mirror_channel_still_uses_db(app, client, local_storage):
    """Regression: a channel without mirror_url must keep reading from the DB."""
    _storage, _ = local_storage
    sm = get_sessionmaker()
    async with sm() as session:
        session.add(Channel(name="local", storage_prefix="local"))
        await session.commit()

    # No DB packages, no cached files → empty list, and no storage listing
    # occurred because the non-mirror branch short-circuits.
    resp = await client.get("/api/channels/local/packages")
    assert resp.status_code == 200
    assert resp.json() == []
