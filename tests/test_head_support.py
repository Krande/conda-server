"""HEAD method support on channel file URLs.

Regression test for a bug where pixi/rattler's existence-probe HEAD got
405, causing clients to fall back to their content-addressed local
cache and never actually hit the server.
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
        # path -> (status, headers, body)
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


async def _seed_channel(**kwargs) -> Channel:
    sm = get_sessionmaker()
    async with sm() as session:
        channel = Channel(**kwargs)
        session.add(channel)
        await session.commit()
        await session.refresh(channel)
        return channel


@pytest.mark.asyncio
async def test_head_repodata_empty_channel_200(app, client):
    await _seed_channel(name="plain", storage_prefix="plain")
    resp = await client.head("/plain/linux-64/repodata.json")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_head_package_cached_returns_size(app, client, tmp_path):
    local = ObstoreStorage(LocalStore(str(tmp_path)), supports_signing=False)
    storage_module.set_storage(local)
    try:
        await _seed_channel(name="plain", storage_prefix="plain")
        from conda_server.db import get_sessionmaker
        from conda_server.models import Package, PackageVersion

        sm = get_sessionmaker()
        async with sm() as session:
            channel = (
                await session.execute(
                    __import__("sqlalchemy").select(Channel).where(Channel.name == "plain")
                )
            ).scalar_one()
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
                    size=10,
                )
            )
            await session.commit()

        await local.put("plain/linux-64/foo-1.0-0.conda", b"0123456789")

        resp = await client.head("/plain/linux-64/foo-1.0-0.conda")
        assert resp.status_code == 200
        assert resp.headers["content-length"] == "10"
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_head_mirror_package_forwards_upstream(app, client, tmp_path, stub_upstream):
    """HEAD on an uncached mirror package forwards to upstream without fetching the body."""
    local = ObstoreStorage(LocalStore(str(tmp_path)), supports_signing=False)
    storage_module.set_storage(local)
    try:
        stub_upstream.responses = {
            "/linux-64/xtensor-0.25.0.conda": (200, {"Content-Length": "9999"}, b""),
        }
        await _seed_channel(
            name="cf",
            storage_prefix="cf",
            mirror_url="https://upstream.example",
            mirror_cache_seconds=900,
        )

        resp = await client.head("/cf/linux-64/xtensor-0.25.0.conda")
        assert resp.status_code == 200
        assert resp.headers["content-length"] == "9999"
        # Upstream saw the HEAD, not a GET — no cache-write.
        assert stub_upstream.calls == [("HEAD", "/linux-64/xtensor-0.25.0.conda")]
        # Package was NOT cached as a side effect.
        assert not (tmp_path / "cf" / "linux-64" / "xtensor-0.25.0.conda").exists()
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_head_mirror_missing_upstream_404(app, client, tmp_path, stub_upstream):
    local = ObstoreStorage(LocalStore(str(tmp_path)), supports_signing=False)
    storage_module.set_storage(local)
    try:
        stub_upstream.responses = {}  # everything 404
        await _seed_channel(
            name="cf",
            storage_prefix="cf",
            mirror_url="https://upstream.example",
        )
        resp = await client.head("/cf/linux-64/nope-1.0.conda")
        assert resp.status_code == 404
    finally:
        storage_module.reset_storage()
