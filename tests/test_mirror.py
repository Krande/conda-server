"""Mirror / proxy tests.

Upstream is a stubbed ``httpx.MockTransport``; we inspect hit/miss counts
rather than doing real network I/O. Storage is the ``LocalStorage``
obstore-backed local backend, so we exercise the real head/get/put paths.
"""

from __future__ import annotations

import time

import httpx
import pytest

from conda_server import mirror
from conda_server import storage as storage_module
from conda_server.db import get_sessionmaker
from conda_server.models import Channel


class StubUpstream:
    """Counts requests and returns canned responses keyed by path."""

    def __init__(self, responses: dict[str, tuple[int, bytes]]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        status_code, body = self.responses.get(path, (404, b""))
        return httpx.Response(status_code, content=body)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture
def stub_upstream(request):
    """Install a fresh stub transport as the mirror module's http client.

    Usage: mark the test with ``pytest.mark.parametrize("stub_upstream", [...])``
    or call the fixture directly and wire responses inside the test body.
    """
    stub = StubUpstream(responses={})
    client = httpx.AsyncClient(transport=stub.transport(), follow_redirects=True)
    mirror.set_http_client(client)
    yield stub
    # Reset so other tests don't see our mock.
    mirror.set_http_client(None)


async def _seed_channel(**kwargs) -> Channel:
    sm = get_sessionmaker()
    async with sm() as session:
        channel = Channel(**kwargs)
        session.add(channel)
        await session.commit()
        await session.refresh(channel)
        return channel


@pytest.mark.asyncio
async def test_package_cached_on_first_fetch(app, client, tmp_path, stub_upstream):
    # Real local storage so we can observe the cache.
    local = storage_module.ObstoreStorage(
        storage_module.LocalStore(str(tmp_path)),
        supports_signing=False,
    )
    storage_module.set_storage(local)
    try:
        stub_upstream.responses = {
            "/linux-64/foo-1.0-0.conda": (200, b"FAKE_CONDA_BYTES"),
        }
        await _seed_channel(
            name="cf",
            storage_prefix="cf",
            mirror_url="https://upstream.example",
            mirror_cache_seconds=900,
        )

        # First request: upstream hit, body streamed back.
        resp = await client.get("/cf/linux-64/foo-1.0-0.conda")
        assert resp.status_code == 200
        assert resp.content == b"FAKE_CONDA_BYTES"
        assert stub_upstream.calls == ["/linux-64/foo-1.0-0.conda"]

        # Second request: served from local cache, no additional upstream call.
        resp = await client.get("/cf/linux-64/foo-1.0-0.conda")
        assert resp.status_code == 200
        assert resp.content == b"FAKE_CONDA_BYTES"
        assert stub_upstream.calls == ["/linux-64/foo-1.0-0.conda"]
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_package_404_propagates(app, client, tmp_path, stub_upstream):
    local = storage_module.ObstoreStorage(
        storage_module.LocalStore(str(tmp_path)),
        supports_signing=False,
    )
    storage_module.set_storage(local)
    try:
        stub_upstream.responses = {}  # everything 404
        await _seed_channel(
            name="cf",
            storage_prefix="cf",
            mirror_url="https://upstream.example",
        )
        resp = await client.get("/cf/linux-64/does-not-exist.conda")
        assert resp.status_code == 404
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_repodata_ttl_refreshes(app, client, tmp_path, stub_upstream):
    local = storage_module.ObstoreStorage(
        storage_module.LocalStore(str(tmp_path)),
        supports_signing=False,
    )
    storage_module.set_storage(local)
    try:
        stub_upstream.responses = {
            "/linux-64/repodata.json": (200, b'{"v":1}'),
        }
        await _seed_channel(
            name="cf",
            storage_prefix="cf",
            mirror_url="https://upstream.example",
            mirror_cache_seconds=900,
        )

        r1 = await client.get("/cf/linux-64/repodata.json")
        assert r1.status_code == 200 and r1.content == b'{"v":1}'
        assert len(stub_upstream.calls) == 1

        # Within TTL, no new upstream hit.
        r2 = await client.get("/cf/linux-64/repodata.json")
        assert r2.status_code == 200
        assert len(stub_upstream.calls) == 1

        # Upstream bumps the version; before forcing expiry, client still sees v1.
        stub_upstream.responses["/linux-64/repodata.json"] = (200, b'{"v":2}')
        r3 = await client.get("/cf/linux-64/repodata.json")
        assert r3.content == b'{"v":1}'

        # Simulate TTL expiry by backdating the cached file's mtime.
        cached_path = tmp_path / "cf" / "linux-64" / "repodata.json"
        past = time.time() - 10_000
        import os

        os.utime(cached_path, (past, past))

        r4 = await client.get("/cf/linux-64/repodata.json")
        assert r4.content == b'{"v":2}'
        assert len(stub_upstream.calls) == 2
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_repodata_falls_back_to_stale_on_upstream_error(
    app,
    client,
    tmp_path,
    stub_upstream,
):
    local = storage_module.ObstoreStorage(
        storage_module.LocalStore(str(tmp_path)),
        supports_signing=False,
    )
    storage_module.set_storage(local)
    try:
        stub_upstream.responses = {
            "/linux-64/repodata.json": (200, b'{"v":1}'),
        }
        await _seed_channel(
            name="cf",
            storage_prefix="cf",
            mirror_url="https://upstream.example",
            mirror_cache_seconds=900,
        )

        r1 = await client.get("/cf/linux-64/repodata.json")
        assert r1.status_code == 200 and r1.content == b'{"v":1}'

        # Force TTL expiry.
        cached_path = tmp_path / "cf" / "linux-64" / "repodata.json"
        past = time.time() - 10_000
        import os

        os.utime(cached_path, (past, past))

        # Upstream goes 500; we still serve the stale cached copy.
        stub_upstream.responses["/linux-64/repodata.json"] = (500, b"oops")

        r2 = await client.get("/cf/linux-64/repodata.json")
        assert r2.status_code == 200
        assert r2.content == b'{"v":1}'
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_non_mirror_channel_unchanged_by_mirror_module(
    app,
    client,
    tmp_path,
    stub_upstream,
):
    """Regression: channels without mirror_url must NOT hit upstream."""
    local = storage_module.ObstoreStorage(
        storage_module.LocalStore(str(tmp_path)),
        supports_signing=False,
    )
    storage_module.set_storage(local)
    try:
        await _seed_channel(name="plain", storage_prefix="plain")
        resp = await client.get("/plain/linux-64/foo-1.0-0.conda")
        assert resp.status_code == 404
        assert stub_upstream.calls == []
    finally:
        storage_module.reset_storage()
