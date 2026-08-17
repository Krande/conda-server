"""Sharded repodata (CEP-16) proxy tests.

Covers:
- /repodata_shards.msgpack.zst is served via the repodata TTL cache.
- /shards/<sha256-hex>.msgpack.zst (per-name shard, under the CEP-16
  `shards/` subfolder of the subdir) is served via the content-addressed
  cache and cached forever (same path as packages).
- HEAD on both works without triggering cache-writes.
- Non-mirror channels 404 (we don't generate shards locally — yet).
"""

from __future__ import annotations

import httpx
import pytest

from conda_server import mirror
from conda_server import storage as storage_module
from conda_server.db import get_sessionmaker
from conda_server.models import Channel
from conda_server.storage import LocalStore, ObstoreStorage


class StubUpstream:
    def __init__(self, responses: dict[str, tuple[int, dict[str, str], bytes]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        status_code, headers, body = self.responses.get(request.url.path, (404, {}, b""))
        return httpx.Response(status_code, headers=headers, content=body)


@pytest.fixture
def stub_upstream():
    stub = StubUpstream(responses={})
    client = httpx.AsyncClient(transport=httpx.MockTransport(stub.handler))
    mirror.set_http_client(client)
    yield stub
    mirror.set_http_client(None)


async def _seed_mirror(tmp_path) -> Channel:
    storage = ObstoreStorage(LocalStore(str(tmp_path)), supports_signing=False)
    storage_module.set_storage(storage)
    sm = get_sessionmaker()
    async with sm() as session:
        channel = Channel(
            name="cf",
            storage_prefix="cf",
            mirror_url="https://upstream.example",
            mirror_cache_seconds=900,
        )
        session.add(channel)
        await session.commit()
        await session.refresh(channel)
        return channel


@pytest.mark.asyncio
async def test_shards_index_proxied_and_cached(app, client, tmp_path, stub_upstream):
    try:
        await _seed_mirror(tmp_path)
        stub_upstream.responses = {
            "/linux-64/repodata_shards.msgpack.zst": (200, {}, b"SHARDS_INDEX"),
        }
        r1 = await client.get("/cf/linux-64/repodata_shards.msgpack.zst")
        assert r1.status_code == 200
        assert r1.content == b"SHARDS_INDEX"
        assert r1.headers["content-type"].startswith("application/x-msgpack")

        # Second request within TTL → no new upstream GET.
        before = len(stub_upstream.calls)
        r2 = await client.get("/cf/linux-64/repodata_shards.msgpack.zst")
        assert r2.status_code == 200
        assert r2.content == b"SHARDS_INDEX"
        assert len(stub_upstream.calls) == before
    finally:
        storage_module.reset_storage()


SHARD_HASH_A = "a" * 64
SHARD_HASH_B = "b" * 64
SHARD_HASH_C = "c" * 64


@pytest.mark.asyncio
async def test_shard_blob_cached_forever(app, client, tmp_path, stub_upstream):
    try:
        await _seed_mirror(tmp_path)
        stub_upstream.responses = {
            f"/linux-64/shards/{SHARD_HASH_A}.msgpack.zst": (200, {}, b"SHARD_BODY"),
        }
        r1 = await client.get(f"/cf/linux-64/shards/{SHARD_HASH_A}.msgpack.zst")
        assert r1.status_code == 200
        assert r1.content == b"SHARD_BODY"

        # Same shard again — served from local cache, no upstream call.
        before = len(stub_upstream.calls)
        r2 = await client.get(f"/cf/linux-64/shards/{SHARD_HASH_A}.msgpack.zst")
        assert r2.status_code == 200
        assert r2.content == b"SHARD_BODY"
        assert len(stub_upstream.calls) == before
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_head_shard_forwards_upstream(app, client, tmp_path, stub_upstream):
    try:
        await _seed_mirror(tmp_path)
        shard_path = f"/linux-64/shards/{SHARD_HASH_B}.msgpack.zst"
        stub_upstream.responses = {
            shard_path: (200, {"Content-Length": "42"}, b""),
        }
        resp = await client.head(f"/cf{shard_path}")
        assert resp.status_code == 200
        assert resp.headers["content-length"] == "42"
        # HEAD forwarded, no GET / cache write.
        assert stub_upstream.calls == [("HEAD", shard_path)]
        assert not (
            tmp_path / "cf" / "linux-64" / "shards" / f"{SHARD_HASH_B}.msgpack.zst"
        ).exists()
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_shard_404_on_non_mirror(app, client, tmp_path, stub_upstream):
    sm = get_sessionmaker()
    async with sm() as session:
        session.add(Channel(name="local", storage_prefix="local"))
        await session.commit()
    resp = await client.get(f"/local/linux-64/shards/{SHARD_HASH_C}.msgpack.zst")
    assert resp.status_code == 404
