"""What a reindex is allowed to cost.

The generic reindex — the route taken by every backend rattler cannot
index in place — used to mirror the whole channel into a temp directory
so ``index_fs`` could be pointed at it. That made the local-disk cost of
publishing one small package equal to the size of the entire channel, and
under a fixed per-pod ephemeral-storage limit it turned every publish
past that limit into a crash partway through, with the artifact already
in object storage and the index not yet updated.

So these are the two properties worth holding onto, and the reason they
are asserted as *bounds* rather than as a particular implementation:

- Disk stays proportional to one archive, never to the channel.
- A package already known to the database costs nothing to republish.

Archives here are real ``.conda`` files — zips of zstd tarballs — because
the metadata path reads them through rattler, and a fake would only prove
the test's own assumptions.
"""

from __future__ import annotations

import io
import json
import os
import tarfile
import zipfile
from pathlib import Path

import pytest
import zstandard

from conda_server import storage as storage_module
from conda_server.config import StorageSettings
from conda_server.db import get_sessionmaker
from conda_server.indexer import _reindex_via_metadata, load_repodata_entries
from conda_server.models import Channel, PackageVersion
from conda_server.package_about import ArchiveFetchError
from conda_server.storage import build_storage

from .test_package_about import FULL_ABOUT, _add

#: Big enough that mirroring the channel is unmistakable next to holding
#: one archive, small enough that the suite stays fast.
_ARCHIVE_PAYLOAD = 512 * 1024
_PACKAGE_COUNT = 6


def _dir_size(root: Path) -> int:
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


def make_archive(version: str, *, payload_size: int) -> bytes:
    """A real ``.conda`` whose ``info/index.json`` declares ``version``.

    The version has to vary per archive rather than only the storage
    filename: the index is built from what each archive says about
    itself, and six files all claiming to be the same version is not a
    channel any of this has to handle.

    The payload is random so it cannot compress to nothing — the tests
    here turn on there being real bytes that must not reach local disk.
    """
    index = {
        "name": "pkg-a",
        "version": version,
        "build": "h0",
        "build_number": 0,
        "subdir": "linux-64",
        "depends": [],
    }

    info = io.BytesIO()
    with tarfile.open(fileobj=info, mode="w") as tf:
        _add(tf, "info/index.json", json.dumps(index).encode())
        _add(tf, "info/about.json", json.dumps(FULL_ABOUT).encode())

    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as tf:
        _add(tf, "lib/pkg_a/blob.bin", os.urandom(payload_size))

    compressor = zstandard.ZstdCompressor()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("metadata.json", json.dumps({"conda_pkg_format_version": 2}))
        zf.writestr(f"info-pkg-a-{version}-h0.tar.zst", compressor.compress(info.getvalue()))
        zf.writestr(f"pkg-pkg-a-{version}-h0.tar.zst", compressor.compress(payload.getvalue()))
    return buf.getvalue()


class _WatchedStorage:
    """Delegates to a real storage, recording disk use around each fetch.

    The footprint is sampled when a fetch *finishes* rather than when it
    starts, because that is when the archive being read is at full size
    on disk — the moment an implementation that accumulates would show
    it. Sampling on entry would see the gap between archives and report
    nothing either way.
    """

    def __init__(self, inner, watch: Path) -> None:
        self._inner = inner
        self._watch = watch
        self.footprints: list[int] = []
        self.fetched_keys: list[str] = []
        #: Keys whose reads raise, standing in for a storage backend
        #: having a bad moment.
        self.unreadable: set[str] = set()

    def __getattr__(self, item):
        return getattr(self._inner, item)

    async def stream(self, key: str):
        self.fetched_keys.append(key)
        if key in self.unreadable:
            raise OSError("backend unavailable")
        async for chunk in self._inner.stream(key):
            yield chunk
        self.footprints.append(_dir_size(self._watch))

    async def get(self, key: str) -> bytes:
        self.fetched_keys.append(key)
        data = await self._inner.get(key)
        self.footprints.append(_dir_size(self._watch))
        return data

    @property
    def peak(self) -> int:
        return max(self.footprints, default=0)

    @property
    def archive_fetches(self) -> list[str]:
        return [k for k in self.fetched_keys if k.endswith((".conda", ".tar.bz2"))]


async def _seed_channel(storage, prefix: str, subdir: str) -> int:
    """Write ``_PACKAGE_COUNT`` real archives into storage. Returns total bytes."""
    total = 0
    for i in range(_PACKAGE_COUNT):
        blob = make_archive(f"1.0.{i}", payload_size=_ARCHIVE_PAYLOAD)
        await storage.put(f"{prefix}/{subdir}/pkg-a-1.0.{i}-h0.conda", blob)
        total += len(blob)
    return total


@pytest.fixture
def watched(tmp_path, monkeypatch):
    """A storage whose temp-file traffic lands in a directory we can measure.

    ``tempfile.tempdir`` is redirected rather than an env var patched so
    the redirect applies to code that resolved ``gettempdir`` differently,
    and the object store is kept outside the watched tree so its own files
    never count as ephemeral use.
    """
    import tempfile as tempfile_module

    watch = tmp_path / "ephemeral"
    watch.mkdir()
    monkeypatch.setattr(tempfile_module, "tempdir", str(watch))

    storage = build_storage(StorageSettings(backend="local", url=str(tmp_path / "objects")))
    wrapped = _WatchedStorage(storage, watch)
    storage_module.set_storage(wrapped)
    try:
        yield wrapped, watch
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_reindex_disk_use_is_bounded_by_one_archive(app, watched):
    """The whole point: disk does not scale with the channel."""
    storage, watch = watched
    channel_bytes = await _seed_channel(storage, "chan", "linux-64")

    sm = get_sessionmaker()
    async with sm() as session:
        channel = Channel(name="chan", storage_prefix="chan")
        session.add(channel)
        await session.commit()
        await _reindex_via_metadata(session, storage, channel)

    one_archive = channel_bytes // _PACKAGE_COUNT

    # Lower bound first, so the upper bound cannot pass by measuring
    # nothing: if the temp redirect stopped working, or the reindex
    # stopped touching disk in a way this can see, that is a broken
    # instrument rather than a fixed bug, and it should say so.
    assert storage.peak >= one_archive, (
        f"expected to observe one archive on disk, saw {storage.peak} bytes — "
        "the measurement is not watching the right directory"
    )
    # Two archives' worth of slack absorbs filesystem block rounding
    # without coming anywhere near the whole channel.
    assert storage.peak < 2 * one_archive, (
        f"reindex held {storage.peak} bytes on disk; "
        f"one archive is ~{one_archive} and the channel is {channel_bytes}"
    )
    # Nothing survives the pass, successful or not.
    assert _dir_size(watch) == 0


@pytest.mark.asyncio
async def test_reindex_publishes_every_package_it_found(app, watched):
    """Bounded cost is worthless if the index comes out wrong."""
    storage, _ = watched
    await _seed_channel(storage, "chan", "linux-64")

    sm = get_sessionmaker()
    async with sm() as session:
        channel = Channel(name="chan", storage_prefix="chan")
        session.add(channel)
        await session.commit()
        await _reindex_via_metadata(session, storage, channel)

    published = json.loads(await storage.get("chan/linux-64/repodata.json"))
    entries = published["packages.conda"]
    assert len(entries) == _PACKAGE_COUNT
    assert published["packages"] == {}

    record = entries["pkg-a-1.0.0-h0.conda"]
    assert record["name"] == "pkg-a"
    assert record["version"] == "1.0.0"
    assert record["build"] == "h0"
    assert record["subdir"] == "linux-64"
    # Hashes come from the same pass that read the metadata, so they
    # describe the bytes actually in storage rather than what a caller
    # claimed to upload.
    assert len(record["sha256"]) == 64
    assert len(record["md5"]) == 32
    assert record["size"] == len(await storage.get("chan/linux-64/pkg-a-1.0.0-h0.conda"))

    # Clients ask for the compressed index first; publishing one without
    # the other serves a stale answer to the fastest clients.
    assert await storage.head("chan/linux-64/repodata.json.zst") is not None


@pytest.mark.asyncio
async def test_reindex_of_an_unchanged_channel_fetches_nothing(app, watched):
    """Steady state is the common case, and it should cost nothing.

    Every package is already recorded, so there is no archive worth
    opening — which is what keeps a reindex from being an expensive
    operation to trigger.
    """
    storage, _ = watched
    await _seed_channel(storage, "chan", "linux-64")

    sm = get_sessionmaker()
    async with sm() as session:
        channel = Channel(name="chan", storage_prefix="chan")
        session.add(channel)
        await session.commit()
        await _reindex_via_metadata(session, storage, channel)
        # Persist what the first pass learned, the way a real reindex does
        # via _sync_db_from_repodata.
        from conda_server.indexer import _sync_db_from_repodata

        await _sync_db_from_repodata(session, storage, channel)
        await session.commit()

        storage.fetched_keys.clear()
        await _reindex_via_metadata(session, storage, channel)

    assert storage.archive_fetches == []


@pytest.mark.asyncio
async def test_reindex_reopens_a_package_whose_bytes_changed(app, watched):
    """A record can only go stale by the artifact being replaced.

    Size is the detector, so a rebuild under the same filename has to be
    re-read rather than trusted from the database.
    """
    storage, _ = watched
    await _seed_channel(storage, "chan", "linux-64")

    sm = get_sessionmaker()
    async with sm() as session:
        channel = Channel(name="chan", storage_prefix="chan")
        session.add(channel)
        await session.commit()
        await _reindex_via_metadata(session, storage, channel)
        from conda_server.indexer import _sync_db_from_repodata

        await _sync_db_from_repodata(session, storage, channel)
        await session.commit()

        replaced = "chan/linux-64/pkg-a-1.0.0-h0.conda"
        await storage.put(replaced, make_archive("1.0.0", payload_size=_ARCHIVE_PAYLOAD * 2))

        storage.fetched_keys.clear()
        await _reindex_via_metadata(session, storage, channel)
        await _sync_db_from_repodata(session, storage, channel)
        await session.commit()

    assert storage.archive_fetches == [replaced]

    published = json.loads(await storage.get("chan/linux-64/repodata.json"))
    record = published["packages.conda"]["pkg-a-1.0.0-h0.conda"]
    assert record["size"] == len(await storage.get(replaced))

    async with sm() as session:
        row = (
            await session.execute(
                PackageVersion.__table__.select().where(
                    PackageVersion.filename == "pkg-a-1.0.0-h0.conda"
                )
            )
        ).first()
        assert row.size == record["size"]


@pytest.mark.asyncio
async def test_reindex_aborts_rather_than_publishing_a_truncated_index(app, watched):
    """A storage failure must not read as "these packages are gone".

    The index is written as a replacement, so quietly continuing past an
    archive that could not be read delists it — the same outcome as
    deleting it, reached by a transient error that says nothing about
    the package.
    """
    storage, _ = watched
    await _seed_channel(storage, "chan", "linux-64")

    sm = get_sessionmaker()
    async with sm() as session:
        channel = Channel(name="chan", storage_prefix="chan")
        session.add(channel)
        await session.commit()
        await _reindex_via_metadata(session, storage, channel)

        before = await storage.get("chan/linux-64/repodata.json")

        # An artifact whose bytes changed, so the record must be re-read —
        # and a backend that will not serve it.
        replaced = "chan/linux-64/pkg-a-1.0.0-h0.conda"
        await storage.put(replaced, make_archive("1.0.0", payload_size=_ARCHIVE_PAYLOAD * 2))
        storage.unreadable.add(replaced)

        with pytest.raises(ArchiveFetchError):
            await _reindex_via_metadata(session, storage, channel)

    # The previously published index is intact — no package was dropped.
    assert await storage.get("chan/linux-64/repodata.json") == before


@pytest.mark.asyncio
async def test_publish_refuses_to_merge_into_an_unreadable_index(app, watched):
    """Merging into an index that will not parse would erase the channel.

    ``load_repodata_entries`` feeds a read-modify-write, so returning
    "nothing" for a corrupt index means the next publish replaces every
    package in the subdir with the one being uploaded.
    """
    storage, _ = watched
    await _seed_channel(storage, "chan", "linux-64")
    await storage.put("chan/linux-64/repodata.json", b"{ truncated")

    with pytest.raises(ValueError, match="reindex"):
        await load_repodata_entries(storage, "chan", "linux-64")

    # A subdir that genuinely has no index is the one case that reads as
    # empty, because there it is also true.
    assert await load_repodata_entries(storage, "chan", "osx-arm64") == {}
