"""The package endpoints hand back versions newest-first, not upload-order.

Regression for the garbled version list on the package page: rows came
out in insertion (upload) order, so a package uploaded 0.10.0 before
0.9.0 listed them that way.
"""

from __future__ import annotations

import json

import pytest

from conda_server import storage as storage_module
from conda_server.db import get_sessionmaker
from conda_server.models import Channel, Package, PackageVersion
from conda_server.storage import LocalStore, ObstoreStorage

# Deliberately not in version order — this is the insertion order the
# endpoint used to echo back.
SEEDED = ["0.9.0", "0.10.0", "0.2.0", "2.1", "2.5.4", "2.5.3"]


async def _seed_package(channel_name: str = "c", package_name: str = "widget") -> Channel:
    sm = get_sessionmaker()
    async with sm() as session:
        channel = Channel(name=channel_name, storage_prefix=channel_name)
        session.add(channel)
        await session.flush()
        pkg = Package(channel_id=channel.id, name=package_name)
        session.add(pkg)
        await session.flush()
        session.add_all(
            [
                PackageVersion(
                    package_id=pkg.id,
                    version=v,
                    build="h0",
                    build_number=0,
                    subdir="linux-64",
                    filename=f"{package_name}-{v}-h0.conda",
                )
                for v in SEEDED
            ]
        )
        await session.commit()
        await session.refresh(channel)
        return channel


@pytest.mark.asyncio
async def test_package_detail_orders_versions_newest_first(app, client):
    channel = await _seed_package()

    resp = await client.get(f"/api/channels/{channel.name}/packages/widget")
    assert resp.status_code == 200
    versions = [v["version"] for v in resp.json()["versions"]]
    assert versions == ["2.5.4", "2.5.3", "2.1", "0.10.0", "0.9.0", "0.2.0"]


@pytest.mark.asyncio
async def test_package_list_orders_versions_newest_first(app, client):
    """The channel list page reads versions[0] as "latest version"."""
    channel = await _seed_package()

    resp = await client.get(f"/api/channels/{channel.name}/packages")
    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["versions"][0]["version"] == "2.5.4"


@pytest.mark.asyncio
async def test_version_order_rank_is_dense_and_newest_first(app, client):
    channel = await _seed_package()

    resp = await client.get(f"/api/channels/{channel.name}/packages/widget")
    ranks = [v["version_order"] for v in resp.json()["versions"]]
    assert ranks == [0, 1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_version_order_is_shared_by_builds_of_one_version(app, client):
    sm = get_sessionmaker()
    async with sm() as session:
        channel = Channel(name="multi", storage_prefix="multi")
        session.add(channel)
        await session.flush()
        pkg = Package(channel_id=channel.id, name="w")
        session.add(pkg)
        await session.flush()
        session.add_all(
            [
                PackageVersion(
                    package_id=pkg.id,
                    version="1.0",
                    build=f"h_{n}",
                    build_number=n,
                    subdir="linux-64",
                    filename=f"w-1.0-h_{n}.conda",
                )
                for n in (0, 2, 1)
            ]
            + [
                PackageVersion(
                    package_id=pkg.id,
                    version="0.9",
                    build="h_0",
                    build_number=0,
                    subdir="linux-64",
                    filename="w-0.9-h_0.conda",
                )
            ]
        )
        await session.commit()

    resp = await client.get("/api/channels/multi/packages/w")
    versions = resp.json()["versions"]
    assert [v["build_number"] for v in versions] == [2, 1, 0, 0]
    assert [v["version_order"] for v in versions] == [0, 0, 0, 1]


@pytest.mark.asyncio
async def test_created_at_is_exposed(app, client):
    """The Added column reads this; the column predates the change, so
    no migration was needed."""
    channel = await _seed_package()

    resp = await client.get(f"/api/channels/{channel.name}/packages/widget")
    for v in resp.json()["versions"]:
        assert v["created_at"], "created_at should be populated by the server default"


@pytest.fixture
def local_storage(tmp_path):
    store = ObstoreStorage(LocalStore(str(tmp_path)), supports_signing=False)
    storage_module.set_storage(store)
    yield store, tmp_path
    storage_module.reset_storage()


@pytest.mark.asyncio
async def test_mirror_channel_versions_are_version_sorted(app, client, local_storage):
    """Mirror listings sorted the filename-derived strings lexicographically."""
    storage, _ = local_storage
    sm = get_sessionmaker()
    async with sm() as session:
        session.add(
            Channel(
                name="cf",
                storage_prefix="cf",
                mirror_url="https://upstream.example",
                mirror_cache_seconds=900,
            )
        )
        await session.commit()

    await storage.put("cf/linux-64/repodata.json", json.dumps({"packages": {}}).encode())
    for v in ("0.9.0", "0.10.0", "0.2.0"):
        await storage.put(f"cf/linux-64/xt-{v}-hf0_0.conda", b"FAKE")

    resp = await client.get("/api/channels/cf/packages/xt")
    assert resp.status_code == 200
    versions = resp.json()["versions"]
    assert [v["version"] for v in versions] == ["0.10.0", "0.9.0", "0.2.0"]
    # Storage last-modified stands in for the missing DB row.
    assert all(v["created_at"] for v in versions)
