"""Package metadata (docs URL, homepage, repository, summary, description).

Covers the two halves of the feature:

* the indexer pulling ``info/about.json`` out of archives in storage, and
  the cost bound that stops it from re-opening archives it has already
  read;
* the packages API collapsing per-version metadata to the one value the
  package page shows, which is a *version-ordering* decision and not an
  upload-order one.

That last point is the trap the ordering tests below exist for: a lower
version can be republished after a higher one already shipped, and
picking by upload time would then advertise the older release's links.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from conda_server import storage as storage_module
from conda_server.config import StorageSettings
from conda_server.db import get_sessionmaker
from conda_server.indexer import _sync_db_from_repodata, capture_about
from conda_server.models import Channel, Package, PackageVersion
from conda_server.storage import build_storage
from tests.test_package_about import FULL_ABOUT, make_conda

DOCS = "https://example.com/docs/pkg-a/"
OLD_DOCS = "https://example.com/docs/pkg-a/v1/"


# --- API: which version's metadata the package page shows --------------


async def _seed(versions: list[dict], channel_name: str = "example-channel") -> Channel:
    """Insert one package with the given version rows, in list order.

    Insertion order is meaningful here: it is the upload order, and
    ``created_at`` is stamped to match so a test can make the *lower*
    version the most recently uploaded one.
    """
    sm = get_sessionmaker()
    async with sm() as session:
        channel = Channel(name=channel_name, storage_prefix=channel_name)
        session.add(channel)
        await session.flush()
        pkg = Package(channel_id=channel.id, name="pkg-a")
        session.add(pkg)
        await session.flush()
        for offset, spec in enumerate(versions):
            version = spec.pop("version")
            session.add(
                PackageVersion(
                    package_id=pkg.id,
                    version=version,
                    build="h0",
                    build_number=0,
                    subdir="linux-64",
                    filename=f"pkg-a-{version}-h0.conda",
                    created_at=datetime(2026, 1, 1, tzinfo=UTC).replace(minute=offset),
                    **spec,
                )
            )
        await session.commit()
        await session.refresh(channel)
        return channel


@pytest.mark.asyncio
async def test_package_shows_newest_versions_metadata(app, client):
    channel = await _seed(
        [
            {"version": "1.0.0", "doc_url": OLD_DOCS, "summary": "old"},
            {"version": "2.0.0", "doc_url": DOCS, "summary": "new"},
        ]
    )

    body = (await client.get(f"/api/channels/{channel.name}/packages/pkg-a")).json()

    assert body["doc_url"] == DOCS
    assert body["summary"] == "new"


@pytest.mark.asyncio
async def test_lower_version_uploaded_later_does_not_win(app, client):
    """The trap: 2.4.0 republished *after* 2.4.1 already shipped.

    Ordering by ``created_at`` — the natural-looking choice, and the one
    that was a bug elsewhere in this codebase — would surface 2.4.0's
    stale links here.
    """
    channel = await _seed(
        [
            {"version": "2.4.1", "doc_url": DOCS, "summary": "current"},
            # Uploaded last, so it has the newest created_at, but it is
            # the older release.
            {"version": "2.4.0", "doc_url": OLD_DOCS, "summary": "stale"},
        ]
    )

    body = (await client.get(f"/api/channels/{channel.name}/packages/pkg-a")).json()

    assert body["doc_url"] == DOCS
    assert body["summary"] == "current"


@pytest.mark.asyncio
async def test_conda_ordering_not_string_ordering(app, client):
    """0.10.0 is newer than 0.9.0, which string ordering gets backwards."""
    channel = await _seed(
        [
            {"version": "0.10.0", "doc_url": DOCS},
            {"version": "0.9.0", "doc_url": OLD_DOCS},
        ]
    )

    body = (await client.get(f"/api/channels/{channel.name}/packages/pkg-a")).json()

    assert body["doc_url"] == DOCS


@pytest.mark.asyncio
async def test_package_without_any_metadata_is_all_null(app, client):
    channel = await _seed([{"version": "1.0.0"}, {"version": "2.0.0"}])

    body = (await client.get(f"/api/channels/{channel.name}/packages/pkg-a")).json()

    assert body["doc_url"] is None
    assert body["home"] is None
    assert body["dev_url"] is None
    assert body["summary"] is None
    assert body["description"] is None


@pytest.mark.asyncio
async def test_partial_metadata_leaves_the_rest_null(app, client):
    channel = await _seed([{"version": "1.0.0", "home": "https://example.com/pkg-a"}])

    body = (await client.get(f"/api/channels/{channel.name}/packages/pkg-a")).json()

    assert body["home"] == "https://example.com/pkg-a"
    assert body["doc_url"] is None
    assert body["summary"] is None


@pytest.mark.asyncio
async def test_falls_back_to_an_older_version_that_has_metadata(app, client):
    """Rollout case: the newest version predates metadata capture.

    Showing the older release's docs link beats showing none, and it can
    only happen when the newer version has nothing at all — among
    versions that do carry metadata, conda ordering still decides.
    """
    channel = await _seed(
        [
            {"version": "1.0.0", "doc_url": OLD_DOCS},
            {"version": "2.0.0"},
        ]
    )

    body = (await client.get(f"/api/channels/{channel.name}/packages/pkg-a")).json()

    assert body["doc_url"] == OLD_DOCS


@pytest.mark.asyncio
async def test_package_list_carries_metadata_too(app, client):
    channel = await _seed([{"version": "1.0.0", "doc_url": DOCS}])

    body = (await client.get(f"/api/channels/{channel.name}/packages")).json()

    assert body[0]["doc_url"] == DOCS


# --- indexer: reading about.json out of storage ------------------------


def _repodata(filename: str, sha256: str, size: int) -> bytes:
    return json.dumps(
        {
            "info": {"subdir": "linux-64"},
            "packages": {},
            "packages.conda": {
                filename: {
                    "name": "pkg-a",
                    "version": "1.0.0",
                    "build": "h0",
                    "build_number": 0,
                    "size": size,
                    "sha256": sha256,
                    "md5": "d" * 32,
                    "depends": [],
                    "constrains": [],
                }
            },
            "repodata_version": 2,
        }
    ).encode()


class _CountingStorage:
    """Delegating wrapper that counts archive reads.

    The point of the indexer's cost bound is a number of storage reads,
    so the test asserts on that number rather than on wall time.
    """

    def __init__(self, inner):
        self._inner = inner
        self.stream_calls: list[str] = []

    def __getattr__(self, item):
        return getattr(self._inner, item)

    def stream(self, key: str):
        self.stream_calls.append(key)
        return self._inner.stream(key)


async def _prepare_channel(tmp_path, about: dict | None, name: str = "idx"):
    """Local-backend storage holding one real archive plus its repodata."""
    archive = make_conda(tmp_path / "pkg-a-1.0.0-h0.conda", about)
    payload = archive.read_bytes()

    storage = _CountingStorage(build_storage(StorageSettings(backend="local", url=str(tmp_path))))
    storage_module.set_storage(storage)
    await storage.put(f"{name}/linux-64/pkg-a-1.0.0-h0.conda", payload)
    await storage.put(
        f"{name}/linux-64/repodata.json",
        _repodata("pkg-a-1.0.0-h0.conda", "a" * 64, len(payload)),
    )
    return storage, payload


@pytest.mark.asyncio
async def test_indexer_captures_about_for_new_rows(app, tmp_path):
    storage, _ = await _prepare_channel(tmp_path, FULL_ABOUT)
    try:
        sm = get_sessionmaker()
        async with sm() as session:
            channel = Channel(name="idx", storage_prefix="idx")
            session.add(channel)
            await session.commit()
            await _sync_db_from_repodata(session, storage, channel)
            await session.commit()

        async with sm() as session:
            row = (await session.execute(PackageVersion.__table__.select())).first()
            assert row.doc_url == DOCS
            assert row.summary == "A small example package."
            assert row.about_fetched_at is not None
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_indexer_stamps_archives_without_about_json(app, tmp_path):
    """A package with no about.json must index fine and stay blank."""
    storage, _ = await _prepare_channel(tmp_path, None)
    try:
        sm = get_sessionmaker()
        async with sm() as session:
            channel = Channel(name="idx", storage_prefix="idx")
            session.add(channel)
            await session.commit()
            stats = await _sync_db_from_repodata(session, storage, channel)
            await session.commit()

        assert stats == {"added": 1, "updated": 0, "removed": 0}

        async with sm() as session:
            row = (await session.execute(PackageVersion.__table__.select())).first()
            assert row.doc_url is None
            # Stamped anyway, so the backfill command won't re-download it.
            assert row.about_fetched_at is not None
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_reindex_does_not_reopen_unchanged_archives(app, tmp_path):
    """The cost bound: a steady-state reindex reads zero archives.

    Without this, every reindex would pull every artifact in the channel
    out of storage just to re-read a few KB it already has.
    """
    storage, _ = await _prepare_channel(tmp_path, FULL_ABOUT)
    try:
        sm = get_sessionmaker()
        async with sm() as session:
            channel = Channel(name="idx", storage_prefix="idx")
            session.add(channel)
            await session.commit()
            await _sync_db_from_repodata(session, storage, channel)
            await session.commit()

        assert len(storage.stream_calls) == 1, "first pass reads the new archive once"

        async with sm() as session:
            channel = await session.get(Channel, channel.id)
            stats = await _sync_db_from_repodata(session, storage, channel)
            await session.commit()

        assert stats == {"added": 0, "updated": 0, "removed": 0}
        assert len(storage.stream_calls) == 1, "second pass must read no archives"
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_backfill_reads_archives_for_unstamped_rows(app, tmp_path):
    """What the ``backfill-about`` command does to a pre-existing row."""
    storage, payload = await _prepare_channel(tmp_path, FULL_ABOUT)
    try:
        sm = get_sessionmaker()
        async with sm() as session:
            channel = Channel(name="idx", storage_prefix="idx")
            session.add(channel)
            await session.flush()
            pkg = Package(channel_id=channel.id, name="pkg-a")
            session.add(pkg)
            await session.flush()
            row = PackageVersion(
                package_id=pkg.id,
                version="1.0.0",
                build="h0",
                build_number=0,
                subdir="linux-64",
                filename="pkg-a-1.0.0-h0.conda",
                size=len(payload),
            )
            session.add(row)
            await session.commit()

            assert await capture_about(storage, "idx", row) is True
            await session.commit()
            assert row.doc_url == DOCS
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_capture_leaves_row_unstamped_when_the_object_is_missing(app, tmp_path):
    """A storage failure is transient — it must not be remembered as "no metadata"."""
    storage = build_storage(StorageSettings(backend="local", url=str(tmp_path)))
    row = PackageVersion(
        package_id=1,
        version="1.0.0",
        build="h0",
        build_number=0,
        subdir="linux-64",
        filename="absent-1.0.0-h0.conda",
    )

    assert await capture_about(storage, "idx", row) is False
    assert row.about_fetched_at is None


@pytest.mark.asyncio
async def test_oversized_archive_is_skipped_but_stamped(app, tmp_path):
    """The size cap is policy, not failure, so retrying would be pointless."""
    from conda_server.indexer import MAX_ABOUT_ARCHIVE_BYTES

    storage = build_storage(StorageSettings(backend="local", url=str(tmp_path)))
    row = PackageVersion(
        package_id=1,
        version="1.0.0",
        build="h0",
        build_number=0,
        subdir="linux-64",
        filename="huge-1.0.0-h0.conda",
        size=MAX_ABOUT_ARCHIVE_BYTES + 1,
    )

    assert await capture_about(storage, "idx", row) is True
    assert row.about_fetched_at is not None
    assert row.doc_url is None


@pytest.mark.asyncio
async def test_inline_created_row_is_not_re_read_by_the_next_reindex(app, tmp_path):
    """The import path's row already has metadata but no sha256 yet.

    That makes the following reindex see it as "changed". Without the
    stamp check it would then fetch the whole artifact back out of
    storage to re-learn what the /tmp copy already told us.
    """
    storage, payload = await _prepare_channel(tmp_path, FULL_ABOUT)
    try:
        sm = get_sessionmaker()
        async with sm() as session:
            channel = Channel(name="idx", storage_prefix="idx")
            session.add(channel)
            await session.flush()
            pkg = Package(channel_id=channel.id, name="pkg-a")
            session.add(pkg)
            await session.flush()
            session.add(
                PackageVersion(
                    package_id=pkg.id,
                    version="1.0.0",
                    build="h0",
                    build_number=0,
                    subdir="linux-64",
                    filename="pkg-a-1.0.0-h0.conda",
                    size=len(payload),
                    # What the import path writes: metadata read from the
                    # spooled copy, stamped, but no sha256 to compare on.
                    doc_url=DOCS,
                    about_fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
            )
            await session.commit()

            channel = await session.get(Channel, channel.id)
            stats = await _sync_db_from_repodata(session, storage, channel)
            await session.commit()

        assert stats == {"added": 0, "updated": 1, "removed": 0}
        assert storage.stream_calls == [], "no archive should be re-read"

        async with sm() as session:
            row = (await session.execute(PackageVersion.__table__.select())).first()
            assert row.doc_url == DOCS
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_replaced_bytes_trigger_a_fresh_read(app, tmp_path):
    """A genuinely republished artifact may have moved its docs URL."""
    storage, payload = await _prepare_channel(tmp_path, FULL_ABOUT)
    try:
        sm = get_sessionmaker()
        async with sm() as session:
            channel = Channel(name="idx", storage_prefix="idx")
            session.add(channel)
            await session.flush()
            pkg = Package(channel_id=channel.id, name="pkg-a")
            session.add(pkg)
            await session.flush()
            session.add(
                PackageVersion(
                    package_id=pkg.id,
                    version="1.0.0",
                    build="h0",
                    build_number=0,
                    subdir="linux-64",
                    filename="pkg-a-1.0.0-h0.conda",
                    size=len(payload),
                    sha256="f" * 64,  # differs from the repodata sha256
                    doc_url=OLD_DOCS,
                    about_fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
            )
            await session.commit()

            channel = await session.get(Channel, channel.id)
            await _sync_db_from_repodata(session, storage, channel)
            await session.commit()

        assert len(storage.stream_calls) == 1

        async with sm() as session:
            row = (await session.execute(PackageVersion.__table__.select())).first()
            assert row.doc_url == DOCS
    finally:
        storage_module.reset_storage()
