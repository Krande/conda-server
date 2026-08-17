"""Upstream search + import-from-upstream flow.

The upstream itself is stubbed via httpx.MockTransport — same pattern
the mirror tests already use. Shard payloads are real msgpack+zstd
blobs so the decode path actually runs.
"""

from __future__ import annotations

import asyncio
import hashlib
from unittest.mock import patch

import httpx
import msgpack
import pytest
import zstandard
from sqlalchemy import select

from conda_server import mirror
from conda_server import storage as storage_module
from conda_server.db import get_sessionmaker
from conda_server.models import Channel, ChannelMember, Package, PackageVersion, User
from conda_server.storage import LocalStore, ObstoreStorage
from tests.conftest import make_session_cookie


def _msgpack_zst(obj: object) -> bytes:
    raw = msgpack.packb(obj, use_bin_type=True)
    return zstandard.ZstdCompressor().compress(raw)


def _shard_for(name: str, versions: list[dict]) -> bytes:
    """Build a CEP-16-shaped per-name shard with .conda entries."""
    packages_conda = {
        f"{name}-{v['version']}-{v['build']}.conda".encode(): {
            b"name": name.encode(),
            b"version": v["version"].encode(),
            b"build": v["build"].encode(),
            b"build_number": v.get("build_number", 0),
            b"subdir": v["subdir"].encode(),
            b"size": v.get("size", 1234),
            b"sha256": v.get("sha256", "a" * 64).encode(),
            b"depends": [d.encode() for d in v.get("depends", [])],
        }
        for v in versions
    }
    return _msgpack_zst({b"packages.conda": packages_conda})


def _shards_index(per_name: dict[str, bytes]) -> tuple[bytes, dict[str, bytes]]:
    """Build a shards index mapping name → sha256(shard_bytes).

    Returns (encoded_index, sha_hex_to_shard_bytes) so the test transport
    can serve each shard from `shards_dir/<sha>.msgpack.zst`.
    """
    name_to_hash: dict[bytes, bytes] = {}
    sha_hex_to_shard: dict[str, bytes] = {}
    for name, shard_bytes in per_name.items():
        sha = hashlib.sha256(shard_bytes).digest()
        name_to_hash[name.encode()] = sha
        sha_hex_to_shard[sha.hex()] = shard_bytes
    encoded = _msgpack_zst(
        {
            b"info": {b"subdir": b"linux-64"},
            b"shards_base_url": b"",
            b"repodata_version": 2,
            b"shards": name_to_hash,
        }
    )
    return encoded, sha_hex_to_shard


class _Upstream:
    """Minimal in-memory upstream serving a shards index + shards + .conda bytes."""

    def __init__(self) -> None:
        self.shards_index: dict[str, bytes] = {}  # path -> bytes
        self.objects: dict[str, bytes] = {}  # path -> bytes
        self.requests: list[str] = []

    def respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url.path)
        body = self.shards_index.get(request.url.path) or self.objects.get(request.url.path)
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, content=body)


@pytest.fixture
def stub_upstream():
    # Drop any shards-index cache built by a previous test — it's
    # process-global state and the URL is the same across tests.
    from conda_server.api import upstream as upstream_api

    upstream_api._shards_cache.clear()
    up = _Upstream()
    client = httpx.AsyncClient(transport=httpx.MockTransport(up.respond))
    mirror.set_http_client(client)
    yield up
    mirror.set_http_client(None)
    upstream_api._shards_cache.clear()


async def _seed_user(role: str = "user") -> User:
    sm = get_sessionmaker()
    async with sm() as session:
        u = User(subject=f"u-{role}", email=f"{role}@x", username=role, role=role)
        session.add(u)
        await session.commit()
        await session.refresh(u)
        return u


async def _seed_channel(name: str, owner: User | None = None) -> Channel:
    sm = get_sessionmaker()
    async with sm() as session:
        ch = Channel(name=name, storage_prefix=name, private=False)
        session.add(ch)
        await session.flush()
        if owner is not None and owner.role != "admin":
            session.add(ChannelMember(channel_id=ch.id, user_id=owner.id, role="owner"))
        await session.commit()
        await session.refresh(ch)
        return ch


def _seed_upstream_with_one(stub: _Upstream, name: str = "hdf5") -> dict:
    versions = [
        {"version": "2.1.0", "build": "h0_0", "subdir": "linux-64", "size": 1024},
        {"version": "1.14.0", "build": "h0_0", "subdir": "linux-64", "size": 1000},
    ]
    shard = _shard_for(name, versions)
    encoded_index, shard_map = _shards_index({name: shard})
    stub.shards_index["/linux-64/repodata_shards.msgpack.zst"] = encoded_index
    for sha_hex, shard_bytes in shard_map.items():
        stub.objects[f"/linux-64/{sha_hex}.msgpack.zst"] = shard_bytes
    return {"name": name, "versions": versions}


# -------- search ---------------------------------------------------------


@pytest.mark.asyncio
async def test_search_substring_match(app, client, stub_upstream):
    _seed_upstream_with_one(stub_upstream, "hdf5")
    user = await _seed_user(role="admin")
    cookie = make_session_cookie(user.subject)
    resp = await client.get(
        "/api/upstream/search?url=https://up.example&subdir=linux-64&name=hd",
        cookies={"session": cookie},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["matched"] == 1
    assert body["packages"][0]["name"] == "hdf5"


@pytest.mark.asyncio
async def test_search_returns_empty_for_no_match(app, client, stub_upstream):
    _seed_upstream_with_one(stub_upstream, "hdf5")
    user = await _seed_user(role="admin")
    cookie = make_session_cookie(user.subject)
    resp = await client.get(
        "/api/upstream/search?url=https://up.example&subdir=linux-64&name=zzz",
        cookies={"session": cookie},
    )
    assert resp.status_code == 200
    assert resp.json()["matched"] == 0


@pytest.mark.asyncio
async def test_search_requires_auth(app, client, stub_upstream):
    _seed_upstream_with_one(stub_upstream)
    resp = await client.get("/api/upstream/search?url=https://up.example&subdir=linux-64&name=hd")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_search_404_when_upstream_lacks_shards(app, client, stub_upstream):
    user = await _seed_user(role="admin")
    cookie = make_session_cookie(user.subject)
    resp = await client.get(
        "/api/upstream/search?url=https://up.example&subdir=linux-64&name=anything",
        cookies={"session": cookie},
    )
    assert resp.status_code == 404
    assert "sharded" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_search_signals_local_presence(app, client, stub_upstream):
    _seed_upstream_with_one(stub_upstream, "hdf5")
    user = await _seed_user(role="admin")
    ch = await _seed_channel("local-ch", owner=user)
    sm = get_sessionmaker()
    async with sm() as session:
        session.add(Package(channel_id=ch.id, name="hdf5"))
        await session.commit()

    cookie = make_session_cookie(user.subject)
    resp = await client.get(
        "/api/upstream/search?url=https://up.example&subdir=linux-64&name=hdf",
        cookies={"session": cookie},
    )
    assert resp.json()["packages"][0]["in_channels"] == ["local-ch"]


# -------- versions -------------------------------------------------------


@pytest.mark.asyncio
async def test_versions_returns_published_set(app, client, stub_upstream):
    seeded = _seed_upstream_with_one(stub_upstream, "hdf5")
    user = await _seed_user(role="admin")
    cookie = make_session_cookie(user.subject)
    resp = await client.get(
        "/api/upstream/versions?url=https://up.example&subdir=linux-64&name=hdf5",
        cookies={"session": cookie},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    versions = sorted(v["version"] for v in body["versions"])
    assert versions == sorted(v["version"] for v in seeded["versions"])
    # in_target_channel defaults to false when no target_channel given.
    assert all(v["in_target_channel"] is False for v in body["versions"])


@pytest.mark.asyncio
async def test_versions_marks_in_target_channel(app, client, stub_upstream):
    _seed_upstream_with_one(stub_upstream, "hdf5")
    user = await _seed_user(role="admin")
    ch = await _seed_channel("dest", owner=user)
    sm = get_sessionmaker()
    async with sm() as session:
        pkg = Package(channel_id=ch.id, name="hdf5")
        session.add(pkg)
        await session.flush()
        session.add(
            PackageVersion(
                package_id=pkg.id,
                version="2.1.0",
                build="h0_0",
                subdir="linux-64",
                filename="hdf5-2.1.0-h0_0.conda",
            )
        )
        await session.commit()

    cookie = make_session_cookie(user.subject)
    resp = await client.get(
        "/api/upstream/versions?url=https://up.example&subdir=linux-64&name=hdf5&target_channel=dest",
        cookies={"session": cookie},
    )
    body = resp.json()
    by_filename = {v["filename"]: v for v in body["versions"]}
    assert by_filename["hdf5-2.1.0-h0_0.conda"]["in_target_channel"] is True
    assert by_filename["hdf5-1.14.0-h0_0.conda"]["in_target_channel"] is False


# -------- import ---------------------------------------------------------


def _stub_index(name: str, version: str, build: str, subdir: str = "linux-64"):
    class _Stub:
        pass

    s = _Stub()
    s.name = name
    s.version = version
    s.build = build
    s.subdir = subdir
    s.build_number = 0
    s.timestamp = None
    s.depends = []
    s.constrains = []
    return s


async def _await_import_jobs() -> None:
    """Block until every in-flight import background task has finished.

    The ``POST /import`` endpoint is asynchronous: it enqueues an
    ``ImportJob`` and kicks off ``_run_import_job`` via
    ``asyncio.create_task`` (tracked in ``channels._RUNNING_IMPORTS``),
    returning ``202`` immediately. Tests drive that task to completion
    here — while any ``patch`` context is still active and, crucially,
    before the ``app`` fixture disposes the engine — then observe the
    results on the job row via the status endpoint.
    """
    from conda_server.api import channels as channels_api

    while channels_api._RUNNING_IMPORTS:
        await asyncio.gather(*list(channels_api._RUNNING_IMPORTS))


@pytest.mark.asyncio
async def test_import_pulls_upstream_into_storage(app, client, stub_upstream, tmp_path):
    user = await _seed_user(role="admin")
    await _seed_channel("dest", owner=user)
    cookie = make_session_cookie(user.subject)

    fake_bytes = b"FAKE_CONDA_BYTES" * 64  # 1 KiB
    stub_upstream.objects["/linux-64/hdf5-2.1.0-h0_0.conda"] = fake_bytes

    store = ObstoreStorage(LocalStore(str(tmp_path)), supports_signing=False)
    storage_module.set_storage(store)
    try:
        with (
            patch(
                "conda_server.api.channels.rattler.IndexJson.from_package_archive",
                return_value=_stub_index("hdf5", "2.1.0", "h0_0"),
            ),
            patch("conda_server.api.channels._reindex_background"),
        ):
            resp = await client.post(
                "/api/channels/dest/import",
                json={
                    "upstream_url": "https://up.example",
                    "packages": [{"subdir": "linux-64", "filename": "hdf5-2.1.0-h0_0.conda"}],
                },
                cookies={"session": cookie},
            )
            assert resp.status_code == 202, resp.text
            job_id = resp.json()["job_id"]
            # The endpoint is async: drive the background import to
            # completion while the patches above are still in force.
            await _await_import_jobs()
        # Results now live on the ImportJob row — observe them via the
        # job-status endpoint the same way a real client would.
        status_resp = await client.get(
            f"/api/channels/dest/import/jobs/{job_id}",
            cookies={"session": cookie},
        )
        assert status_resp.status_code == 200, status_resp.text
        job_body = status_resp.json()
        assert job_body["status"] == "completed"
        result = job_body["results"][0]
        assert result["status"] == "stored"
        assert result["imported_from"] == "https://up.example/linux-64/hdf5-2.1.0-h0_0.conda"
        # Bytes landed in storage.
        stored = await store.get("dest/linux-64/hdf5-2.1.0-h0_0.conda")
        assert stored == fake_bytes
        # Row + provenance landed in DB.
        sm = get_sessionmaker()
        async with sm() as session:
            row = (
                await session.execute(
                    select(PackageVersion).join(Package).where(Package.name == "hdf5")
                )
            ).scalar_one()
            assert row.imported_from == "https://up.example/linux-64/hdf5-2.1.0-h0_0.conda"
            assert row.version == "2.1.0"
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_import_rejects_already_present(app, client, stub_upstream, tmp_path):
    user = await _seed_user(role="admin")
    ch = await _seed_channel("dup", owner=user)
    cookie = make_session_cookie(user.subject)

    stub_upstream.objects["/linux-64/hdf5-2.1.0-h0_0.conda"] = b"X" * 100

    store = ObstoreStorage(LocalStore(str(tmp_path)), supports_signing=False)
    storage_module.set_storage(store)
    try:
        # Pre-seed the destination with the same object so the import rejects.
        await store.put(f"{ch.storage_prefix}/linux-64/hdf5-2.1.0-h0_0.conda", b"existing")
        resp = await client.post(
            "/api/channels/dup/import",
            json={
                "upstream_url": "https://up.example",
                "packages": [{"subdir": "linux-64", "filename": "hdf5-2.1.0-h0_0.conda"}],
            },
            cookies={"session": cookie},
        )
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]
        await _await_import_jobs()
        status_resp = await client.get(
            f"/api/channels/dup/import/jobs/{job_id}",
            cookies={"session": cookie},
        )
        assert status_resp.status_code == 200, status_resp.text
        result = status_resp.json()["results"][0]
        assert result["status"] == "error"
        assert "already" in result["error"].lower()
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_import_rejects_metadata_mismatch(app, client, stub_upstream, tmp_path):
    user = await _seed_user(role="admin")
    await _seed_channel("mm", owner=user)
    cookie = make_session_cookie(user.subject)

    stub_upstream.objects["/linux-64/hdf5-2.1.0-h0_0.conda"] = b"X" * 100

    store = ObstoreStorage(LocalStore(str(tmp_path)), supports_signing=False)
    storage_module.set_storage(store)
    try:
        with patch(
            "conda_server.api.channels.rattler.IndexJson.from_package_archive",
            return_value=_stub_index("evil", "2.1.0", "h0_0"),  # name mismatch
        ):
            resp = await client.post(
                "/api/channels/mm/import",
                json={
                    "upstream_url": "https://up.example",
                    "packages": [{"subdir": "linux-64", "filename": "hdf5-2.1.0-h0_0.conda"}],
                },
                cookies={"session": cookie},
            )
            assert resp.status_code == 202
            job_id = resp.json()["job_id"]
            # Drain inside the patch so the mismatched index is what the
            # background worker sees when it validates the archive.
            await _await_import_jobs()
        status_resp = await client.get(
            f"/api/channels/mm/import/jobs/{job_id}",
            cookies={"session": cookie},
        )
        assert status_resp.status_code == 200, status_resp.text
        result = status_resp.json()["results"][0]
        assert result["status"] == "error"
        assert "disagrees" in result["error"]
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_import_requires_writer(app, client, stub_upstream):
    reader = await _seed_user(role="user")
    ch = await _seed_channel("ro")
    sm = get_sessionmaker()
    async with sm() as session:
        session.add(ChannelMember(channel_id=ch.id, user_id=reader.id, role="reader"))
        await session.commit()
    cookie = make_session_cookie(reader.subject)

    resp = await client.post(
        "/api/channels/ro/import",
        json={
            "upstream_url": "https://up.example",
            "packages": [{"subdir": "linux-64", "filename": "x-1.0-0.conda"}],
        },
        cookies={"session": cookie},
    )
    assert resp.status_code == 403


def _stub_record(
    name: str,
    version: str,
    build: str,
    subdir: str,
    size: int = 1234,
    depends: list[str] | None = None,
):
    """Mimic rattler.RepoDataRecord enough for the preview categorizer."""

    class _Stub:
        pass

    s = _Stub()

    class _Name:
        def __init__(self, n: str):
            self.source = n
            self.normalized = n.lower()

    s.name = _Name(name)
    s.version = version  # str(_) just returns the string
    s.build = build
    s.subdir = subdir
    s.file_name = f"{name}-{version}-{build}.conda"
    s.size = size
    s.depends = depends or []
    return s


@pytest.mark.asyncio
async def test_preview_categorizes_closure(app, client, tmp_path):
    user = await _seed_user(role="admin")
    ch = await _seed_channel("prev", owner=user)
    cookie = make_session_cookie(user.subject)

    # Pre-seed one dep already in the channel so it falls into
    # transitive_satisfied_locally.
    sm = get_sessionmaker()
    async with sm() as session:
        pkg = Package(channel_id=ch.id, name="libgcc")
        session.add(pkg)
        await session.flush()
        session.add(
            PackageVersion(
                package_id=pkg.id,
                version="15.2.0",
                build="he0feb66_18",
                subdir="linux-64",
                filename="libgcc-15.2.0-he0feb66_18.conda",
            )
        )
        await session.commit()

    closure = [
        _stub_record("xtensor", "0.27.1", "h171cf75_0", "linux-64", size=225468),
        _stub_record("xtl", "0.8.2", "h171cf75_0", "linux-64", size=87593),
        _stub_record("libgcc", "15.2.0", "he0feb66_18", "linux-64", size=1041788),
        _stub_record("libstdcxx", "15.2.0", "h934c35e_18", "linux-64", size=5852330),
    ]

    async def fake_solve(*args, **kwargs):
        return closure

    with patch("conda_server.api.channels.rattler.solve", side_effect=fake_solve):
        resp = await client.post(
            "/api/channels/prev/import/preview",
            json={
                "upstream_url": "https://up.example",
                "packages": [
                    {"subdir": "linux-64", "filename": "xtensor-0.27.1-h171cf75_0.conda"},
                ],
            },
            cookies={"session": cookie},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    direct = [p["filename"] for p in body["direct_requested"]]
    new = sorted(p["filename"] for p in body["transitive_new"])
    seen = sorted(p["filename"] for p in body["transitive_satisfied_locally"])
    assert direct == ["xtensor-0.27.1-h171cf75_0.conda"]
    # libgcc is local already; xtl + libstdcxx are new transitive deps.
    assert new == [
        "libstdcxx-15.2.0-h934c35e_18.conda",
        "xtl-0.8.2-h171cf75_0.conda",
    ]
    assert seen == ["libgcc-15.2.0-he0feb66_18.conda"]
    # New bytes only count xtl + libstdcxx, not the local libgcc.
    assert body["total_new_bytes"] == 87593 + 5852330


@pytest.mark.asyncio
async def test_preview_surfaces_solver_error(app, client):
    user = await _seed_user(role="admin")
    await _seed_channel("solverr", owner=user)
    cookie = make_session_cookie(user.subject)

    async def boom(*args, **kwargs):
        raise RuntimeError("xtensor ==9.99 cannot be installed: no candidates")

    with patch("conda_server.api.channels.rattler.solve", side_effect=boom):
        resp = await client.post(
            "/api/channels/solverr/import/preview",
            json={
                "upstream_url": "https://up.example",
                "packages": [
                    {"subdir": "linux-64", "filename": "xtensor-9.99-h0_0.conda"},
                ],
            },
            cookies={"session": cookie},
        )
    assert resp.status_code == 422
    assert "no candidates" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_preview_requires_writer(app, client):
    reader = await _seed_user(role="user")
    ch = await _seed_channel("prevro")
    sm = get_sessionmaker()
    async with sm() as session:
        session.add(ChannelMember(channel_id=ch.id, user_id=reader.id, role="reader"))
        await session.commit()
    cookie = make_session_cookie(reader.subject)
    resp = await client.post(
        "/api/channels/prevro/import/preview",
        json={
            "upstream_url": "https://up.example",
            "packages": [{"subdir": "linux-64", "filename": "x-1.0-0.conda"}],
        },
        cookies={"session": cookie},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_preview_rejects_mirror(app, client):
    user = await _seed_user(role="admin")
    sm = get_sessionmaker()
    async with sm() as session:
        session.add(
            Channel(
                name="prevmir",
                storage_prefix="prevmir",
                mirror_url="https://up.example",
            )
        )
        await session.commit()
    cookie = make_session_cookie(user.subject)
    resp = await client.post(
        "/api/channels/prevmir/import/preview",
        json={
            "upstream_url": "https://up.example",
            "packages": [{"subdir": "linux-64", "filename": "x-1.0-0.conda"}],
        },
        cookies={"session": cookie},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_import_rejects_mirror_channel(app, client, stub_upstream):
    user = await _seed_user(role="admin")
    sm = get_sessionmaker()
    async with sm() as session:
        session.add(
            Channel(
                name="mir",
                storage_prefix="mir",
                mirror_url="https://upstream.example",
            )
        )
        await session.commit()
    cookie = make_session_cookie(user.subject)

    resp = await client.post(
        "/api/channels/mir/import",
        json={
            "upstream_url": "https://up.example",
            "packages": [{"subdir": "linux-64", "filename": "x-1.0-0.conda"}],
        },
        cookies={"session": cookie},
    )
    assert resp.status_code == 400
    assert "mirror" in resp.json()["detail"]
