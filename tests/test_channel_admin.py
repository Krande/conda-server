"""Covers private-channel enforcement plus the admin reindex and delete endpoints."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import select

from conda_server.db import get_sessionmaker
from conda_server.models import Channel, User
from tests.conftest import make_session_cookie


async def _seed_admin(subject: str = "admin1", email: str = "a@x.com") -> User:
    sm = get_sessionmaker()
    async with sm() as session:
        user = User(subject=subject, email=email, username="admin", role="admin")
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def _seed_channel(name: str, private: bool = False) -> Channel:
    sm = get_sessionmaker()
    async with sm() as session:
        channel = Channel(name=name, storage_prefix=name, private=private)
        session.add(channel)
        await session.commit()
        await session.refresh(channel)
        return channel


@pytest.mark.asyncio
async def test_private_channel_hidden_from_anonymous(app, client):
    await _seed_channel("public-a", private=False)
    await _seed_channel("secret", private=True)

    listing = await client.get("/api/channels")
    names = [c["name"] for c in listing.json()]
    assert "public-a" in names
    assert "secret" not in names

    detail = await client.get("/api/channels/secret")
    assert detail.status_code == 404

    packages = await client.get("/api/channels/secret/packages")
    assert packages.status_code == 404

    repodata = await client.get("/secret/linux-64/repodata.json")
    assert repodata.status_code == 404


@pytest.mark.asyncio
async def test_private_channel_visible_to_authenticated(app, client):
    user = await _seed_admin(subject="u-see", email="u@x")
    await _seed_channel("public-b", private=False)
    await _seed_channel("secret", private=True)
    cookie = make_session_cookie(user.subject)

    listing = await client.get("/api/channels", cookies={"session": cookie})
    names = [c["name"] for c in listing.json()]
    assert "public-b" in names and "secret" in names

    detail = await client.get("/api/channels/secret", cookies={"session": cookie})
    assert detail.status_code == 200
    assert detail.json()["private"] is True


@pytest.mark.asyncio
async def test_reindex_requires_admin(app, client):
    await _seed_channel("c1")
    # Anonymous → 401
    resp = await client.post("/api/channels/c1/reindex")
    assert resp.status_code == 401

    # Non-admin user → 403
    sm = get_sessionmaker()
    async with sm() as session:
        session.add(User(subject="regular", email="r@x", username="r", role="user"))
        await session.commit()
    resp = await client.post(
        "/api/channels/c1/reindex", cookies={"session": make_session_cookie("regular")}
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_reindex_queues_background_task(app, client):
    admin = await _seed_admin()
    await _seed_channel("c1")
    cookie = make_session_cookie(admin.subject)

    # Patch the background helper so the test doesn't actually hit storage/rattler.
    with patch("conda_server.api.channels._reindex_background") as mocked:
        resp = await client.post("/api/channels/c1/reindex", cookies={"session": cookie})
    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted", "channel": "c1"}
    # BackgroundTasks invokes after the response; assert it was scheduled by
    # inspecting the mock's call count (FastAPI runs it via asyncio.run in tests).
    assert mocked.call_count == 1
    # The verify flag rides along; a plain trigger is the cheap pass.
    assert mocked.call_args.args == ("c1", False)


@pytest.mark.asyncio
async def test_reindex_missing_channel_404(app, client):
    admin = await _seed_admin()
    cookie = make_session_cookie(admin.subject)
    resp = await client.post("/api/channels/does-not-exist/reindex", cookies={"session": cookie})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_channel_admin_only(app, client):
    await _seed_channel("keep")
    resp = await client.delete("/api/channels/keep")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_delete_channel_removes_row(app, client):
    admin = await _seed_admin()
    await _seed_channel("gone")
    cookie = make_session_cookie(admin.subject)

    resp = await client.delete("/api/channels/gone", cookies={"session": cookie})
    assert resp.status_code == 204

    # Channel really gone.
    follow = await client.get("/api/channels/gone", cookies={"session": cookie})
    assert follow.status_code == 404


@pytest.mark.asyncio
async def test_delete_channel_wipes_storage(app, client, tmp_path):
    """Background task deletes every object under the channel's prefix."""
    from conda_server import storage as storage_module
    from conda_server.storage import LocalStore, ObstoreStorage

    admin = await _seed_admin()
    channel = await _seed_channel("wipe")
    cookie = make_session_cookie(admin.subject)

    store = ObstoreStorage(LocalStore(str(tmp_path)), supports_signing=False)
    storage_module.set_storage(store)
    try:
        # Seed a handful of blobs under the channel's prefix + one elsewhere
        # so we can assert the sibling survives.
        await store.put(f"{channel.storage_prefix}/linux-64/one.conda", b"A")
        await store.put(f"{channel.storage_prefix}/linux-64/two.conda", b"B")
        await store.put(f"{channel.storage_prefix}/repodata.json", b"{}")
        await store.put("other-channel/noarch/leave-me.conda", b"C")

        resp = await client.delete("/api/channels/wipe", cookies={"session": cookie})
        assert resp.status_code == 204

        # The channel's storage is empty.
        async def _count(prefix: str) -> int:
            n = 0
            async for _ in store.list(prefix):
                n += 1
            return n

        assert await _count(f"{channel.storage_prefix}/") == 0
        # Unrelated channel is intact.
        assert await _count("other-channel/") == 1
    finally:
        storage_module.reset_storage()


def _stub_index(
    subdir: str = "linux-64",
    name: str = "x",
    version: str = "1.0",
    build: str = "0",
):
    """A stand-in for ``rattler.IndexJson``.

    Carries every field the upload path reads, including the ones that
    only matter once the record reaches repodata. A stub missing them
    does not fail loudly — the handler catches the AttributeError and
    files it as a per-file error — so an incomplete one here would report
    the upload as stored while quietly publishing nothing.
    """

    class _Stub:
        pass

    stub = _Stub()
    stub.subdir = subdir
    stub.name = name
    stub.version = version
    stub.build = build
    stub.build_number = 0
    stub.depends = []
    stub.constrains = []
    stub.timestamp = None
    stub.license = None
    stub.license_family = None
    stub.platform = None
    stub.arch = None
    stub.track_features = None
    return stub


async def _published(store, prefix: str, subdir: str) -> dict:
    """The records repodata.json currently lists for a subdir, by filename."""
    import json

    raw = await store.get(f"{prefix}/{subdir}/repodata.json")
    data = json.loads(raw)
    return {**(data.get("packages") or {}), **(data.get("packages.conda") or {})}


@pytest.mark.asyncio
async def test_upload_extracts_subdir_and_stores(app, client, tmp_path):
    from conda_server import storage as storage_module
    from conda_server.storage import LocalStore, ObstoreStorage

    admin = await _seed_admin()
    await _seed_channel("up")
    cookie = make_session_cookie(admin.subject)

    store = ObstoreStorage(LocalStore(str(tmp_path)), supports_signing=False)
    storage_module.set_storage(store)
    try:
        body = b"FAKE_CONDA_BYTES" * 1024
        files = [("files", ("xtensor-0.25.0-hf036a51_0.conda", body, "application/octet-stream"))]
        with (
            patch(
                "conda_server.api.channels.rattler.IndexJson.from_package_archive",
                return_value=_stub_index(
                    subdir="linux-64", name="xtensor", version="0.25.0", build="hf036a51_0"
                ),
            ),
            patch("conda_server.api.channels._reindex_background") as mocked_rx,
        ):
            resp = await client.post(
                "/api/channels/up/packages",
                files=files,
                cookies={"session": cookie},
            )
        assert resp.status_code == 202
        payload = resp.json()
        assert payload["channel"] == "up"
        assert len(payload["results"]) == 1
        result = payload["results"][0]
        assert result["status"] == "stored"
        assert result["subdir"] == "linux-64"
        assert result["size"] == len(body)
        assert result["name"] == "xtensor"
        assert "error" not in result

        stored = await store.get("up/linux-64/xtensor-0.25.0-hf036a51_0.conda")
        assert stored == body

        # The response already means "listed". No background task ran, and
        # none needs to: the package is in repodata by the time the client
        # is told the upload succeeded.
        assert mocked_rx.call_count == 0
        published = await _published(store, "up", "linux-64")
        assert "xtensor-0.25.0-hf036a51_0.conda" in published
        assert published["xtensor-0.25.0-hf036a51_0.conda"]["size"] == len(body)
        assert await store.head("up/linux-64/repodata.json.zst") is not None
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_upload_multiple_files_in_one_request(app, client, tmp_path):
    from conda_server import storage as storage_module
    from conda_server.storage import LocalStore, ObstoreStorage

    admin = await _seed_admin()
    await _seed_channel("multi")
    cookie = make_session_cookie(admin.subject)

    store = ObstoreStorage(LocalStore(str(tmp_path)), supports_signing=False)
    storage_module.set_storage(store)
    try:
        files = [
            ("files", ("linpkg-1.0-0.conda", b"LINUX_BYTES", "application/octet-stream")),
            ("files", ("arch-2.0-0.conda", b"ARM_BYTES", "application/octet-stream")),
            ("files", ("nopkg-3.0-0.conda", b"NOARCH_BYTES", "application/octet-stream")),
        ]

        def fake_index(path):
            # The handler strips original filenames, so peek at the temp
            # file's bytes to dispatch the right subdir back.
            with open(path, "rb") as f:
                head = f.read(16)
            if head.startswith(b"LINUX"):
                return _stub_index("linux-64", "linpkg", "1.0", "0")
            if head.startswith(b"ARM"):
                return _stub_index("osx-arm64", "arch", "2.0", "0")
            return _stub_index("noarch", "nopkg", "3.0", "0")

        with (
            patch(
                "conda_server.api.channels.rattler.IndexJson.from_package_archive",
                side_effect=fake_index,
            ),
            patch("conda_server.api.channels._reindex_background") as mocked_rx,
        ):
            resp = await client.post(
                "/api/channels/multi/packages",
                files=files,
                cookies={"session": cookie},
            )
        assert resp.status_code == 202
        results = resp.json()["results"]
        assert len(results) == 3
        assert all(r["status"] == "stored" for r in results)

        for filename, subdir in [
            ("linpkg-1.0-0.conda", "linux-64"),
            ("arch-2.0-0.conda", "osx-arm64"),
            ("nopkg-3.0-0.conda", "noarch"),
        ]:
            stored = await store.get(f"multi/{subdir}/{filename}")
            assert stored is not None and len(stored) > 0
            assert filename in await _published(store, "multi", subdir)

        assert mocked_rx.call_count == 0
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_upload_partial_failure_still_stores_good_file(app, client, tmp_path):
    from conda_server import storage as storage_module
    from conda_server.storage import LocalStore, ObstoreStorage

    admin = await _seed_admin()
    await _seed_channel("part")
    cookie = make_session_cookie(admin.subject)

    store = ObstoreStorage(LocalStore(str(tmp_path)), supports_signing=False)
    storage_module.set_storage(store)
    try:
        files = [
            ("files", ("good-1.0-0.conda", b"GOOD_BYTES", "application/octet-stream")),
            ("files", ("bad-1.0-0.conda", b"BAD_BYTES", "application/octet-stream")),
        ]

        def fake_index(path):
            with open(path, "rb") as f:
                if f.read(4) == b"GOOD":
                    return _stub_index("linux-64", "good", "1.0", "0")
            raise RuntimeError("corrupt archive")

        with (
            patch(
                "conda_server.api.channels.rattler.IndexJson.from_package_archive",
                side_effect=fake_index,
            ),
            patch("conda_server.api.channels._reindex_background") as mocked_rx,
        ):
            resp = await client.post(
                "/api/channels/part/packages",
                files=files,
                cookies={"session": cookie},
            )
        assert resp.status_code == 202
        results = resp.json()["results"]
        by_name = {r["filename"]: r for r in results}
        assert by_name["good-1.0-0.conda"]["status"] == "stored"
        assert by_name["bad-1.0-0.conda"]["status"] == "error"
        assert "corrupt" in by_name["bad-1.0-0.conda"]["error"]
        assert mocked_rx.call_count == 0
        assert await store.head("part/linux-64/good-1.0-0.conda") is not None
        # The good file is published even though its neighbour was not.
        assert "good-1.0-0.conda" in await _published(store, "part", "linux-64")
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_upload_skips_reindex_when_all_fail(app, client, tmp_path):
    from conda_server import storage as storage_module
    from conda_server.storage import LocalStore, ObstoreStorage

    admin = await _seed_admin()
    await _seed_channel("allbad")
    cookie = make_session_cookie(admin.subject)

    store = ObstoreStorage(LocalStore(str(tmp_path)), supports_signing=False)
    storage_module.set_storage(store)
    try:
        files = [("files", ("x-1.0-0.conda", b"NOPE", "application/octet-stream"))]
        with (
            patch(
                "conda_server.api.channels.rattler.IndexJson.from_package_archive",
                side_effect=RuntimeError("unreadable"),
            ),
            patch("conda_server.api.channels._reindex_background") as mocked_rx,
        ):
            resp = await client.post(
                "/api/channels/allbad/packages",
                files=files,
                cookies={"session": cookie},
            )
        assert resp.status_code == 202
        assert resp.json()["results"][0]["status"] == "error"
        assert mocked_rx.call_count == 0
        # Nothing stored means nothing published — no empty index written.
        assert await store.head("allbad/linux-64/repodata.json") is None
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_upload_requires_admin(app, client):
    await _seed_channel("up2")
    resp = await client.post(
        "/api/channels/up2/packages",
        files=[("files", ("x-1.0-0.conda", b"", "application/octet-stream"))],
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_upload_rejects_mirror_channels(app, client):
    admin = await _seed_admin()
    sm = get_sessionmaker()
    async with sm() as session:
        session.add(
            Channel(
                name="cf",
                storage_prefix="cf",
                mirror_url="https://upstream.example",
            )
        )
        await session.commit()
    cookie = make_session_cookie(admin.subject)

    resp = await client.post(
        "/api/channels/cf/packages",
        files=[("files", ("x-1.0-0.conda", b"", "application/octet-stream"))],
        cookies={"session": cookie},
    )
    assert resp.status_code == 400
    assert "mirror" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_upload_rejects_bad_filename(app, client):
    admin = await _seed_admin()
    await _seed_channel("up3")
    cookie = make_session_cookie(admin.subject)

    for bad in [
        "nodots",
        "missing-version.conda",
        "../escape-1.0-0.conda",
        "sub/dir-1.0-0.conda",
        "x-1.0-0.exe",
    ]:
        with patch(
            "conda_server.api.channels.rattler.IndexJson.from_package_archive",
            return_value=_stub_index(),
        ):
            resp = await client.post(
                "/api/channels/up3/packages",
                files=[("files", (bad, b"BYTES", "application/octet-stream"))],
                cookies={"session": cookie},
            )
        assert resp.status_code == 202, f"unexpected status for {bad!r}"
        result = resp.json()["results"][0]
        assert result["status"] == "error", f"expected error for {bad!r}, got {result}"


@pytest.mark.asyncio
async def test_delete_package_removes_bytes_rows_and_queues_reindex(app, client, tmp_path):
    from conda_server import storage as storage_module
    from conda_server.models import Package, PackageVersion
    from conda_server.storage import LocalStore, ObstoreStorage

    admin = await _seed_admin()
    channel = await _seed_channel("dl")
    cookie = make_session_cookie(admin.subject)

    store = ObstoreStorage(LocalStore(str(tmp_path)), supports_signing=False)
    storage_module.set_storage(store)
    try:
        # Seed a package + two versions, and the .conda bytes for one of them.
        filename = "x-1.0-0.conda"
        other = "x-1.1-0.conda"
        sm = get_sessionmaker()
        async with sm() as session:
            pkg = Package(channel_id=channel.id, name="x")
            session.add(pkg)
            await session.flush()
            session.add_all(
                [
                    PackageVersion(
                        package_id=pkg.id,
                        version="1.0",
                        build="0",
                        subdir="linux-64",
                        filename=filename,
                    ),
                    PackageVersion(
                        package_id=pkg.id,
                        version="1.1",
                        build="0",
                        subdir="linux-64",
                        filename=other,
                    ),
                ]
            )
            await session.commit()

        await store.put(f"{channel.storage_prefix}/linux-64/{filename}", b"FAKE_BYTES")

        with patch("conda_server.api.channels._reindex_background") as mocked:
            resp = await client.delete(
                f"/api/channels/{channel.name}/packages/linux-64/{filename}",
                cookies={"session": cookie},
            )
        assert resp.status_code == 204
        assert mocked.call_count == 1
        assert mocked.call_args.args == (channel.name,)

        # Bytes gone.
        assert await store.head(f"{channel.storage_prefix}/linux-64/{filename}") is None
        # Row gone, sibling row intact.
        async with sm() as session:
            remaining = await session.execute(
                select(PackageVersion).where(PackageVersion.filename.in_([filename, other]))
            )
            names = {v.filename for v in remaining.scalars()}
            assert names == {other}
            # Package row still exists because there's still a version.
            survivor = await session.execute(select(Package).where(Package.name == "x"))
            assert survivor.scalar_one_or_none() is not None
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_delete_package_removes_empty_package_row(app, client, tmp_path):
    from conda_server import storage as storage_module
    from conda_server.models import Package, PackageVersion
    from conda_server.storage import LocalStore, ObstoreStorage

    admin = await _seed_admin()
    channel = await _seed_channel("dl2")
    cookie = make_session_cookie(admin.subject)

    store = ObstoreStorage(LocalStore(str(tmp_path)), supports_signing=False)
    storage_module.set_storage(store)
    try:
        sm = get_sessionmaker()
        filename = "only-1.0-0.conda"
        async with sm() as session:
            pkg = Package(channel_id=channel.id, name="only")
            session.add(pkg)
            await session.flush()
            session.add(
                PackageVersion(
                    package_id=pkg.id,
                    version="1.0",
                    build="0",
                    subdir="noarch",
                    filename=filename,
                )
            )
            await session.commit()

        with patch("conda_server.api.channels._reindex_background"):
            resp = await client.delete(
                f"/api/channels/{channel.name}/packages/noarch/{filename}",
                cookies={"session": cookie},
            )
        assert resp.status_code == 204

        async with sm() as session:
            result = await session.execute(select(Package).where(Package.name == "only"))
            assert result.scalar_one_or_none() is None
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_delete_package_rejects_mirror(app, client):
    admin = await _seed_admin()
    sm = get_sessionmaker()
    async with sm() as session:
        session.add(
            Channel(
                name="cf-del",
                storage_prefix="cf-del",
                mirror_url="https://upstream.example",
            )
        )
        await session.commit()
    cookie = make_session_cookie(admin.subject)

    resp = await client.delete(
        "/api/channels/cf-del/packages/noarch/x-1.0-0.conda",
        cookies={"session": cookie},
    )
    assert resp.status_code == 400
    assert "mirror" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_delete_package_requires_admin(app, client):
    await _seed_channel("dl3")
    resp = await client.delete("/api/channels/dl3/packages/noarch/x-1.0-0.conda")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_delete_package_404_when_missing(app, client):
    admin = await _seed_admin()
    await _seed_channel("dl4")
    cookie = make_session_cookie(admin.subject)
    resp = await client.delete(
        "/api/channels/dl4/packages/noarch/nope-1.0-0.conda",
        cookies={"session": cookie},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_package_rejects_traversal(app, client):
    admin = await _seed_admin()
    await _seed_channel("dl5")
    cookie = make_session_cookie(admin.subject)
    # FastAPI encodes the path param, but subdir-level '..' still needs rejecting.
    resp = await client.delete(
        "/api/channels/dl5/packages/noarch/..-1.0-0.conda",
        cookies={"session": cookie},
    )
    # Either 400 from our validation, or 404 because the row doesn't exist —
    # both are acceptable. What we want is "no accidental deletion of the
    # wrong path".
    assert resp.status_code in (400, 404)


@pytest.mark.asyncio
async def test_upload_per_file_size_limit(app, client, monkeypatch):
    import tempfile

    from conda_server import config
    from conda_server import storage as storage_module
    from conda_server.storage import LocalStore, ObstoreStorage

    admin = await _seed_admin()
    await _seed_channel("sz")
    cookie = make_session_cookie(admin.subject)

    # Tiny per-file cap. The multi-MB body should trip it before rattler is called.
    settings = config.get_settings()
    monkeypatch.setattr(settings.upload, "max_file_bytes", 1024)  # 1 KiB

    with tempfile.TemporaryDirectory() as tmp:
        storage_module.set_storage(ObstoreStorage(LocalStore(tmp), supports_signing=False))
        try:
            body = b"X" * 4096  # 4 KiB > cap
            resp = await client.post(
                "/api/channels/sz/packages",
                files=[("files", ("x-1.0-0.conda", body, "application/octet-stream"))],
                cookies={"session": cookie},
            )
            assert resp.status_code == 202
            result = resp.json()["results"][0]
            assert result["status"] == "error"
            assert "limit" in result["error"].lower()
        finally:
            storage_module.reset_storage()


@pytest.mark.asyncio
async def test_upload_total_size_limit(app, client, monkeypatch):
    from conda_server import config

    admin = await _seed_admin()
    await _seed_channel("szt")
    cookie = make_session_cookie(admin.subject)

    settings = config.get_settings()
    monkeypatch.setattr(settings.upload, "max_total_bytes", 2048)  # 2 KiB aggregate

    body = b"Y" * 4096  # each file 4 KiB; first one alone blows the budget
    resp = await client.post(
        "/api/channels/szt/packages",
        files=[
            ("files", ("a-1.0-0.conda", body, "application/octet-stream")),
            ("files", ("b-1.0-0.conda", body, "application/octet-stream")),
        ],
        cookies={"session": cookie},
    )
    # Declared Content-Length already exceeds aggregate cap → 413 before spooling.
    assert resp.status_code == 413
    assert "aggregate" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_upload_rejects_unknown_subdir_from_archive(app, client):
    """Subdir comes from the archive, so a bogus one is a per-file error."""
    admin = await _seed_admin()
    await _seed_channel("up4")
    cookie = make_session_cookie(admin.subject)
    with patch(
        "conda_server.api.channels.rattler.IndexJson.from_package_archive",
        return_value=_stub_index(subdir="solaris-sparc"),
    ):
        resp = await client.post(
            "/api/channels/up4/packages",
            files=[("files", ("x-1.0-0.conda", b"BYTES", "application/octet-stream"))],
            cookies={"session": cookie},
        )
    assert resp.status_code == 202
    result = resp.json()["results"][0]
    assert result["status"] == "error"
    assert "subdir" in result["error"]


@pytest.mark.asyncio
async def test_reupload_republishes_the_record_for_the_new_bytes(app, client, tmp_path):
    """Same filename, different archive — the index must follow the bytes.

    CI that rebuilds a package under a stable version+build-string is a
    supported workflow here, and the build is not byte-reproducible, so
    the second upload is a genuinely different artifact under an
    unchanged name. Rejecting it would break the publisher; taking the
    bytes while keeping the old record breaks every installer, because
    repodata then advertises a hash the download cannot reproduce. The
    row, the published record and the object all have to agree.
    """
    import hashlib

    from sqlalchemy import select

    from conda_server import storage as storage_module
    from conda_server.models import PackageVersion
    from conda_server.storage import LocalStore, ObstoreStorage

    admin = await _seed_admin()
    await _seed_channel("rebuild")
    cookie = make_session_cookie(admin.subject)

    store = ObstoreStorage(LocalStore(str(tmp_path)), supports_signing=False)
    storage_module.set_storage(store)
    try:
        filename = "demopkg-1.2.0-h1234567_1.conda"
        first = b"BUILD_ONE" * 512
        # Not a truncation or a resend: a different artifact, of a
        # different length, under the same name.
        second = b"BUILD_TWO" * 512 + b"_RERUN"

        for body in (first, second):
            with (
                patch(
                    "conda_server.api.channels.rattler.IndexJson.from_package_archive",
                    return_value=_stub_index(
                        subdir="linux-64", name="demopkg", version="1.2.0", build="h1234567_1"
                    ),
                ),
                patch("conda_server.api.channels._reindex_background"),
            ):
                resp = await client.post(
                    "/api/channels/rebuild/packages",
                    files=[("files", (filename, body, "application/octet-stream"))],
                    cookies={"session": cookie},
                )
            assert resp.status_code == 202
            assert resp.json()["results"][0]["status"] == "stored"

        # The re-upload is reported as a replacement rather than passed
        # off as a first publish.
        assert resp.json()["results"][0]["replaced"] is True

        stored = await store.get(f"rebuild/linux-64/{filename}")
        assert stored == second
        actual_sha = hashlib.sha256(second).hexdigest()

        published = await _published(store, "rebuild", "linux-64")
        assert published[filename]["sha256"] == actual_sha
        assert published[filename]["size"] == len(second)

        sm = get_sessionmaker()
        async with sm() as session:
            row = (
                await session.execute(
                    select(PackageVersion).where(PackageVersion.filename == filename)
                )
            ).scalar_one()
            assert row.sha256 == actual_sha
            assert row.size == len(second)
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_upload_drops_the_shard_index_it_cannot_update(app, client, tmp_path):
    """The sharded index is the one pixi reads first.

    A reindex leaves ``repodata_shards.msgpack.zst`` behind and nothing
    on the upload path can rewrite it, so publishing over a filename it
    covers has to retire it — otherwise the corrected repodata.json is
    served only to the clients that ask for the slower index.
    """
    from conda_server import storage as storage_module
    from conda_server.storage import LocalStore, ObstoreStorage

    admin = await _seed_admin()
    await _seed_channel("sharded")
    cookie = make_session_cookie(admin.subject)

    store = ObstoreStorage(LocalStore(str(tmp_path)), supports_signing=False)
    storage_module.set_storage(store)
    try:
        await store.put("sharded/linux-64/repodata_shards.msgpack.zst", b"stale-shard-index")
        with (
            patch(
                "conda_server.api.channels.rattler.IndexJson.from_package_archive",
                return_value=_stub_index(
                    subdir="linux-64", name="xtensor", version="0.25.0", build="hf036a51_0"
                ),
            ),
            patch("conda_server.api.channels._reindex_background"),
        ):
            resp = await client.post(
                "/api/channels/sharded/packages",
                files=[
                    (
                        "files",
                        (
                            "xtensor-0.25.0-hf036a51_0.conda",
                            b"BYTES" * 64,
                            "application/octet-stream",
                        ),
                    )
                ],
                cookies={"session": cookie},
            )
        assert resp.status_code == 202
        assert await store.head("sharded/linux-64/repodata_shards.msgpack.zst") is None
    finally:
        storage_module.reset_storage()
