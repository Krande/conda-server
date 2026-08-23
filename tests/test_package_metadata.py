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
from conda_server.indexer import PACKAGE_SUFFIXES, _sync_db_from_repodata, capture_about
from conda_server.models import Channel, Package, PackageVersion
from conda_server.storage import build_storage
from tests.test_package_about import FULL_ABOUT, make_conda, make_conda_blob

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
    """Delegating wrapper that counts archive opens.

    The point of the indexer's cost bound is a number of archives opened,
    so the tests assert on that rather than on wall time. Both capture
    paths are counted at their first storage call — ``head`` for the
    ranged ``.conda`` path, ``stream`` for the spooled ``.tar.bz2`` one —
    so one entry still means one archive whichever path ran. Non-archive
    keys (repodata) are ignored.
    """

    def __init__(self, inner):
        self._inner = inner
        self.archive_opens: list[str] = []

    def __getattr__(self, item):
        return getattr(self._inner, item)

    def _record(self, key: str) -> None:
        if key.endswith(PACKAGE_SUFFIXES):
            self.archive_opens.append(key)

    async def head(self, key: str):
        self._record(key)
        return await self._inner.head(key)

    def stream(self, key: str):
        self._record(key)
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

        assert len(storage.archive_opens) == 1, "first pass reads the new archive once"

        async with sm() as session:
            channel = await session.get(Channel, channel.id)
            stats = await _sync_db_from_repodata(session, storage, channel)
            await session.commit()

        assert stats == {"added": 0, "updated": 0, "removed": 0}
        assert len(storage.archive_opens) == 1, "second pass must read no archives"
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
async def test_oversized_legacy_archive_is_skipped_but_stamped(app, tmp_path):
    """The size cap is policy, not failure, so retrying would be pointless.

    It applies to ``.tar.bz2`` only, which is the format that has to be
    spooled to disk in full before its metadata can be reached.
    """
    from conda_server.indexer import MAX_ABOUT_ARCHIVE_BYTES

    storage = build_storage(StorageSettings(backend="local", url=str(tmp_path)))
    row = PackageVersion(
        package_id=1,
        version="1.0.0",
        build="h0",
        build_number=0,
        subdir="linux-64",
        filename="huge-1.0.0-h0.tar.bz2",
        size=MAX_ABOUT_ARCHIVE_BYTES + 1,
    )

    assert await capture_about(storage, "idx", row) is True
    assert row.about_fetched_at is not None
    assert row.doc_url is None


class _ByteCountingStorage:
    """Delegating wrapper that totals the archive bytes actually moved.

    Counting opens is the cost bound; counting *bytes* is the other half,
    and the one the ranged path exists for.
    """

    def __init__(self, inner):
        self._inner = inner
        self.fetched = 0

    def __getattr__(self, item):
        return getattr(self._inner, item)

    async def get_range(self, key: str, *, start: int, length: int) -> bytes:
        chunk = await self._inner.get_range(key, start=start, length=length)
        self.fetched += len(chunk)
        return chunk

    async def stream(self, key: str):
        async for chunk in self._inner.stream(key):
            self.fetched += len(chunk)
            yield chunk


async def _store_conda(tmp_path, blob: bytes) -> _ByteCountingStorage:
    storage = _ByteCountingStorage(
        build_storage(StorageSettings(backend="local", url=str(tmp_path)))
    )
    await storage.put("idx/linux-64/pkg-a-1.0.0-h0.conda", blob)
    return storage


def _conda_row(size: int) -> PackageVersion:
    return PackageVersion(
        package_id=1,
        version="1.0.0",
        build="h0",
        build_number=0,
        subdir="linux-64",
        filename="pkg-a-1.0.0-h0.conda",
        size=size,
    )


@pytest.mark.asyncio
async def test_capturing_a_conda_moves_a_fraction_of_the_archive(app, tmp_path):
    """Reading a docs link must not cost the price of the package.

    Spooling the archive to a temporary file to reach a few hundred bytes
    in its tail put the whole artifact on the container's ephemeral disk
    — a shared, hard limit, and one an indexing pass hits once per newly
    published package.
    """
    blob = make_conda_blob(FULL_ABOUT, payload_size=4 * 1024 * 1024)
    storage = await _store_conda(tmp_path, blob)
    row = _conda_row(len(blob))

    assert await capture_about(storage, "idx", row) is True

    assert row.doc_url == DOCS
    assert storage.fetched < 128 * 1024, (
        f"moved {storage.fetched} bytes of a {len(blob)}-byte archive"
    )


@pytest.mark.asyncio
async def test_the_size_cap_does_not_apply_to_conda_archives(app, tmp_path, monkeypatch):
    """A ranged read costs the same whatever the package weighs.

    The cap exists because the legacy format has to be downloaded in full.
    Applying it to ``.conda`` would decline metadata for exactly the
    packages it is cheapest to read, and save nothing.
    """
    import conda_server.indexer as indexer_module

    blob = make_conda_blob(FULL_ABOUT, payload_size=64 * 1024)
    monkeypatch.setattr(indexer_module, "MAX_ABOUT_ARCHIVE_BYTES", 1024)
    storage = await _store_conda(tmp_path, blob)
    row = _conda_row(len(blob))
    assert row.size > 1024

    assert await capture_about(storage, "idx", row) is True
    assert row.doc_url == DOCS


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
        assert storage.archive_opens == [], "no archive should be re-read"

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

        assert len(storage.archive_opens) == 1

        async with sm() as session:
            row = (await session.execute(PackageVersion.__table__.select())).first()
            assert row.doc_url == DOCS
    finally:
        storage_module.reset_storage()


# --- indexer: which versions get opened at all -------------------------
#
# The page renders one version's metadata per package — the newest by
# conda ordering, see ``_about_source`` — so capturing any other version
# buys nothing. A channel typically holds several versions of every
# package, and that multiple was exactly the waste.


def _repodata_for(subdir: str, entries: dict[str, tuple[str, str, int]]) -> bytes:
    """``{filename: (version, sha256, size)}`` as one subdir's repodata."""
    return json.dumps(
        {
            "info": {"subdir": subdir},
            "packages": {},
            "packages.conda": {
                filename: {
                    "name": "pkg-a",
                    "version": version,
                    "build": "h0",
                    "build_number": 0,
                    "size": size,
                    "sha256": sha256,
                    "md5": "d" * 32,
                    "depends": [],
                    "constrains": [],
                }
                for filename, (version, sha256, size) in entries.items()
            },
            "repodata_version": 2,
        }
    ).encode()


async def _publish(storage, specs: list[tuple[str, str, str]], name: str = "idx") -> None:
    """Put one ``.conda`` per spec into storage and rewrite the repodata.

    ``specs`` is ``(subdir, version, sha256)``. Rewriting the whole
    repodata each time is what a real reindex sees, and it lets a test
    change one artifact's sha256 to mean "these bytes were replaced".
    """
    by_subdir: dict[str, dict[str, tuple[str, str, int]]] = {}
    for subdir, version, sha256 in specs:
        filename = f"pkg-a-{version}-h0.conda"
        blob = make_conda_blob(FULL_ABOUT)
        await storage.put(f"{name}/{subdir}/{filename}", blob)
        by_subdir.setdefault(subdir, {})[filename] = (version, sha256, len(blob))
    for subdir, entries in by_subdir.items():
        await storage.put(f"{name}/{subdir}/repodata.json", _repodata_for(subdir, entries))


async def _channel_with(tmp_path, specs: list[tuple[str, str, str]]):
    storage = _CountingStorage(build_storage(StorageSettings(backend="local", url=str(tmp_path))))
    storage_module.set_storage(storage)
    await _publish(storage, specs)

    sm = get_sessionmaker()
    async with sm() as session:
        channel = Channel(name="idx", storage_prefix="idx")
        session.add(channel)
        await session.commit()
        await session.refresh(channel)
    return storage, channel


async def _reindex(channel, storage) -> dict[str, int]:
    sm = get_sessionmaker()
    async with sm() as session:
        chan = await session.get(Channel, channel.id)
        stats = await _sync_db_from_repodata(session, storage, chan)
        await session.commit()
        return stats


async def _rows_by_version() -> dict[str, list]:
    sm = get_sessionmaker()
    async with sm() as session:
        rows = list((await session.execute(PackageVersion.__table__.select())).all())
    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault(row.version, []).append(row)
    return grouped


@pytest.mark.asyncio
async def test_only_the_newest_version_is_opened(app, tmp_path):
    """Three versions land at once; one of them is worth reading."""
    storage, channel = await _channel_with(
        tmp_path,
        [
            ("linux-64", "1.0.0", "a" * 64),
            ("linux-64", "1.9.0", "b" * 64),
            ("linux-64", "2.0.0", "c" * 64),
        ],
    )
    try:
        await _reindex(channel, storage)

        assert storage.archive_opens == ["idx/linux-64/pkg-a-2.0.0-h0.conda"]

        rows = await _rows_by_version()
        assert rows["2.0.0"][0].doc_url == DOCS
        assert rows["1.0.0"][0].doc_url is None
        assert rows["1.0.0"][0].about_fetched_at is None, (
            "an unopened version must stay unstamped, so a backfill can still reach it"
        )
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_string_ordering_does_not_decide_which_version_is_newest(app, tmp_path):
    """0.10.0 is newer than 0.9.0. Reading the wrong archive would still
    produce a plausible-looking page, so this is asserted directly."""
    storage, channel = await _channel_with(
        tmp_path,
        [("linux-64", "0.9.0", "a" * 64), ("linux-64", "0.10.0", "b" * 64)],
    )
    try:
        await _reindex(channel, storage)

        assert storage.archive_opens == ["idx/linux-64/pkg-a-0.10.0-h0.conda"]
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_every_artifact_of_the_newest_version_is_opened(app, tmp_path):
    """One version can be several artifacts. Opening only one of them
    would leave the page's metadata depending on which subdir happened to
    sort first, and on that artifact never being removed."""
    storage, channel = await _channel_with(
        tmp_path,
        [
            ("linux-64", "1.0.0", "a" * 64),
            ("linux-64", "2.0.0", "b" * 64),
            ("win-64", "2.0.0", "c" * 64),
        ],
    )
    try:
        await _reindex(channel, storage)

        assert sorted(storage.archive_opens) == [
            "idx/linux-64/pkg-a-2.0.0-h0.conda",
            "idx/win-64/pkg-a-2.0.0-h0.conda",
        ]
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_a_new_newest_version_is_opened_when_it_lands(app, tmp_path):
    """Newest is a property of the moment, not of the channel.

    Capturing only the newest version is correct only if a version that
    *becomes* the newest is captured at the moment it does.
    """
    storage, channel = await _channel_with(tmp_path, [("linux-64", "1.0.0", "a" * 64)])
    try:
        await _reindex(channel, storage)
        assert storage.archive_opens == ["idx/linux-64/pkg-a-1.0.0-h0.conda"]

        await _publish(storage, [("linux-64", "2.0.0", "b" * 64)])
        storage.archive_opens.clear()
        stats = await _reindex(channel, storage)

        assert stats["added"] == 1
        assert storage.archive_opens == ["idx/linux-64/pkg-a-2.0.0-h0.conda"], (
            "the version that just became newest must be read, and the old one not re-read"
        )
        rows = await _rows_by_version()
        assert rows["2.0.0"][0].doc_url == DOCS
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_a_rebuild_of_an_older_version_does_not_get_opened(app, tmp_path):
    """The trap the ordering rules exist for, in capture form.

    A rebuild of 1.0.0 lands after 2.0.0 already shipped. It is the most
    recent *upload* and the least interesting *version*, and reading it
    would cost an archive to produce metadata nothing renders.
    """
    storage, channel = await _channel_with(
        tmp_path,
        [("linux-64", "1.0.0", "a" * 64), ("linux-64", "2.0.0", "b" * 64)],
    )
    try:
        await _reindex(channel, storage)
        assert storage.archive_opens == ["idx/linux-64/pkg-a-2.0.0-h0.conda"]

        # 1.0.0 republished: same filename, different bytes.
        await _publish(
            storage,
            [("linux-64", "1.0.0", "f" * 64), ("linux-64", "2.0.0", "b" * 64)],
        )
        storage.archive_opens.clear()
        stats = await _reindex(channel, storage)

        assert stats["updated"] == 1
        assert storage.archive_opens == []
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_replaced_bytes_of_the_newest_version_are_re_read(app, tmp_path):
    """The recapture rule still holds where it matters."""
    storage, channel = await _channel_with(
        tmp_path,
        [("linux-64", "1.0.0", "a" * 64), ("linux-64", "2.0.0", "b" * 64)],
    )
    try:
        await _reindex(channel, storage)
        storage.archive_opens.clear()

        await _publish(
            storage,
            [("linux-64", "1.0.0", "a" * 64), ("linux-64", "2.0.0", "e" * 64)],
        )
        await _reindex(channel, storage)

        assert storage.archive_opens == ["idx/linux-64/pkg-a-2.0.0-h0.conda"]
    finally:
        storage_module.reset_storage()
