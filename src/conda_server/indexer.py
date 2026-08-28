"""Channel reindexing — scans object storage, updates repodata.json, syncs DB.

Three routes in, chosen by backend, and the difference between them is
where the archives already are. For the local backend we point rattler's
``index_fs`` at the path — the files are on disk already, so no copy. For
S3 we use ``index_s3``, which works against the bucket in place. Every
other backend has no in-place indexer, and for those we build repodata
ourselves from archive metadata; see ``_reindex_via_metadata`` for why
that is not simply "download the channel and run ``index_fs`` over it".

The constraint the generic route exists to respect: **reindexing must
not scale in disk with the size of the channel.** A server that stages
artifacts locally to index them holds the entire channel on ephemeral
disk, which is a per-pod quota that other things share and that a
container cannot grow. Metadata is kilobytes per package; payloads are
not, and repodata needs none of them.

After indexing, the generated ``repodata.json`` is parsed and the rows in the
``packages`` / ``package_versions`` tables are brought in sync — added,
updated, or deleted. The DB is a cache of the authoritative files on storage.

One field group does *not* come from repodata: the ``info/about.json``
metadata (docs URL, homepage, repository, summary, description). repodata is
built from ``info/index.json`` and carries none of it, so it has to come out
of the archive itself — see ``_capture_about`` for the cost bound that keeps
that from turning every reindex into a full channel download, and
``capture_about`` for how a ``.conda`` gives up its metadata without the
artifact ever leaving object storage.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import rattler
import rattler.index
import zstandard
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from conda_server.config import StorageSettings, get_settings, resolve_path
from conda_server.logging import get_logger
from conda_server.models import Channel, Package, PackageVersion
from conda_server.package_about import (
    ArchiveFetchError,
    PackageAbout,
    read_conda_about_ranged,
    read_package_about,
)
from conda_server.storage import Storage
from conda_server.versions import sort_versions, version_ranks

log = get_logger(__name__)


#: Cap on the ``.tar.bz2`` archives opened for ``about.json``, and only
#: those: the legacy format has to be spooled to local disk in full
#: before its metadata member can be reached, so the cost of reading a
#: docs link is the size of the whole package. ``.conda`` archives are
#: read through byte ranges and are not bounded here — see
#: ``_capture_about_ranged``.
#:
#: Deliberately much smaller than a package can be. Ephemeral disk is a
#: shared, hard, per-container limit and several of these can be in
#: flight at once, so the ceiling has to be one a container can actually
#: absorb rather than one large enough for every conceivable artifact.
#: Legacy archives past it are stamped, not retried.
MAX_ABOUT_ARCHIVE_BYTES = 64 * 1024 * 1024


SUBDIRS = (
    "noarch",
    "linux-64",
    "linux-aarch64",
    "linux-ppc64le",
    "osx-64",
    "osx-arm64",
    "win-64",
)

PACKAGE_SUFFIXES = (".conda", ".tar.bz2")


@dataclass
class IndexResult:
    channel: str
    added: int
    updated: int
    removed: int
    #: Rows whose published hash did not describe the bytes in storage
    #: and were rewritten from the archive. See ``_repair_drifted_records``.
    repaired: int = 0


async def reindex_channel(
    session: AsyncSession,
    storage: Storage,
    channel: Channel,
    *,
    verify: bool = False,
) -> IndexResult:
    """Run rattler-index over the channel's storage prefix and sync the DB.

    ``verify`` re-hashes every archive in the channel instead of only the
    ones a cheap check already suspects. It is off by default because the
    cost is the whole channel's egress; see ``_repair_drifted_records``
    for what the default pass catches without it.
    """
    settings = get_settings().storage
    log.info("reindex.start", channel=channel.name, backend=settings.backend, verify=verify)

    if settings.backend == "local":
        await _reindex_local(settings, channel)
    elif settings.backend == "s3":
        await _reindex_s3(settings, channel)
    else:
        await _reindex_via_metadata(session, storage, channel)

    stats = await _sync_db_from_repodata(session, storage, channel)
    stats["repaired"] = await _repair_drifted_records(session, storage, channel, verify=verify)
    channel.repodata_updated_at = datetime.now(UTC)

    log.info("reindex.done", channel=channel.name, **stats)
    return IndexResult(channel=channel.name, **stats)


async def _repair_drifted_records(
    session: AsyncSession,
    storage: Storage,
    channel: Channel,
    *,
    verify: bool,
) -> int:
    """Rewrite records whose hash no longer describes the bytes in storage.

    Reconciling *which* files exist is what the rest of a reindex does,
    and it is blind to the case where the filename never moved but the
    bytes underneath it did. A CI job that rebuilds a package under a
    stable version+build-string produces exactly that: the archive is
    replaced, the published sha256 is not, and every client that trusts
    the index fails to extract what it downloaded. Nothing upstream of
    here notices — rattler-index leaves a filename already present in
    repodata alone, so the stale record survives every subsequent pass
    and the run reports no changes at all.

    Cost is what decides how much of the channel this is allowed to
    read:

    * The listing already carries every object's size, so one pass over
      it is free and catches any rebuild whose output changed length —
      which is the overwhelmingly common shape of the bug, and the one
      that broke the channel this was written for.
    * A row that has no sha256 at all (the import path creates one
      before the hash is known) is read once and then never again.
    * ``verify`` drops the size gate and re-hashes everything. That is
      the only way to catch a byte-for-byte-length rebuild, and it costs
      the channel's egress, so it stays an explicit operator decision
      rather than the price of a routine reindex.

    Object storage is written before the database, for the same reason
    the upload path does: repodata is what clients read.
    """
    prefix = channel.storage_prefix.strip("/")
    result = await session.execute(
        select(PackageVersion)
        .join(Package, PackageVersion.package_id == Package.id)
        .where(Package.channel_id == channel.id)
    )
    rows = {(v.subdir, v.filename): v for v in result.scalars()}
    if not rows:
        return 0

    sizes: dict[tuple[str, str], int] = {}
    async for meta in storage.list(f"{prefix}/"):
        if not meta.key.endswith(PACKAGE_SUFFIXES):
            continue
        parts = meta.key[len(prefix) :].lstrip("/").split("/")
        if len(parts) != 2:
            continue
        sizes[(parts[0], parts[1])] = meta.size

    fixed: dict[str, dict[str, dict[str, Any]]] = {}
    for (subdir, filename), row in rows.items():
        stored_size = sizes.get((subdir, filename))
        if stored_size is None:
            # A row with no object behind it is a listing question, not a
            # hash one, and _sync_db_from_repodata already owns it.
            continue
        if not verify and row.sha256 and row.size == stored_size:
            continue

        key = f"{prefix}/{subdir}/{filename}"
        fresh = await _entry_from_archive(storage, key, filename, stored_size)
        if fresh is None:
            # Unreadable bytes say nothing about the record, and dropping
            # the package over it would be a bigger claim than we have.
            log.warning("reindex.unreadable_archive", key=key)
            continue
        if fresh.get("sha256") == row.sha256 and fresh.get("size") == row.size:
            continue

        log.warning(
            "reindex.record_drifted",
            channel=channel.name,
            subdir=subdir,
            filename=filename,
            published_sha256=row.sha256,
            actual_sha256=fresh.get("sha256"),
            published_size=row.size,
            actual_size=fresh.get("size"),
        )
        fixed.setdefault(subdir, {})[filename] = fresh

    if not fixed:
        return 0

    for subdir, records in fixed.items():
        entries = await load_repodata_entries(storage, prefix, subdir)
        entries.update(records)
        await write_repodata(storage, prefix, subdir, entries)
        await invalidate_sharded_index(storage, prefix, subdir)

    for subdir, records in fixed.items():
        for filename, info in records.items():
            _apply_version(rows[(subdir, filename)], subdir, filename, info)
    await session.flush()

    return sum(len(records) for records in fixed.values())


async def _reindex_local(settings: StorageSettings, channel: Channel) -> None:
    channel_dir = resolve_path(settings.url) / channel.storage_prefix.strip("/")
    (channel_dir / "noarch").mkdir(parents=True, exist_ok=True)
    await rattler.index.index_fs(channel_dir)


async def _reindex_s3(settings: StorageSettings, channel: Channel) -> None:
    base_url = settings.url.rstrip("/")
    channel_url = f"{base_url}/{channel.storage_prefix.strip('/')}"
    creds = _build_s3_credentials(settings)
    # py-rattler's index_s3 wrapper is typed Optional[S3Credentials] but the
    # Rust binding behind it wants a plain Mapping (TypeError: 'S3Credentials'
    # object cannot be converted to 'Mapping'). Convert the dataclass to a
    # dict at the boundary.
    creds_arg = dataclasses.asdict(creds) if creds is not None else None
    await rattler.index.index_s3(channel_url, credentials=creds_arg)


async def _reindex_via_metadata(
    session: AsyncSession,
    storage: Storage,
    channel: Channel,
) -> None:
    """Generic fallback: rebuild repodata from archive metadata alone.

    **This path must never stage the channel on local disk.** The obvious
    implementation — mirror every artifact into a temp directory, run
    ``index_fs`` over it, upload the result back — is what this replaced,
    and it made the cost of publishing a 25 KB package the size of the
    entire channel. Under a hard per-pod ephemeral-storage limit that is
    not a slow leak: the first upload to a channel larger than the limit
    kills the process mid-request, and since the artifact reaches object
    storage before repodata does, the package is accepted and then
    silently never listed.

    What repodata.json needs is each archive's ``info/index.json`` plus
    its size and hashes — kilobytes per package, none of it in the
    payload. So:

    * A package already recorded in the database contributes its stored
      record and is never fetched. That is the overwhelmingly common
      case, and it makes a steady-state reindex cost zero bytes.
    * A package the database does not know is spooled to a temp file,
      read, and deleted before the next one is considered — so the disk
      high-water mark is one archive, not one channel.

    Size is the change detector: the listing supplies it for free, and
    bytes replaced under an existing filename are the only way a known
    record can go stale.

    A storage failure part-way through aborts the whole pass rather than
    publishing what it managed to collect. The index this writes is a
    replacement, not a patch, so "carry on without the packages that
    could not be read" is the same operation as deleting them — and a
    transient read error is not a statement about a package.
    """
    prefix = channel.storage_prefix.strip("/")
    known = await _known_entries(session, channel)
    found: dict[str, dict[str, dict[str, Any]]] = {}

    async for meta in storage.list(f"{prefix}/"):
        if not meta.key.endswith(PACKAGE_SUFFIXES):
            continue
        rel = meta.key[len(prefix) :].lstrip("/")
        parts = rel.split("/")
        if len(parts) != 2:
            continue
        subdir, filename = parts
        if subdir not in SUBDIRS:
            continue

        entry = known.get((subdir, filename))
        if entry is None or entry.get("size") != meta.size:
            fresh = await _entry_from_archive(storage, meta.key, filename, meta.size)
            if fresh is None:
                # Not a package rattler can read, so not a package any
                # client could have installed. Leaving it out of the
                # index is the accurate answer, not a lossy one.
                log.warning("reindex.unreadable_archive", key=meta.key)
                continue
            entry = fresh
        found.setdefault(subdir, {})[filename] = entry

    for subdir in SUBDIRS:
        entries = found.get(subdir)
        if entries is None and subdir != "noarch":
            # Nothing there and nothing that claims to be there. Leave the
            # subdir absent rather than publishing an empty index for a
            # platform this channel does not target.
            continue
        await write_repodata(storage, prefix, subdir, entries or {})


async def _known_entries(
    session: AsyncSession, channel: Channel
) -> dict[tuple[str, str], dict[str, Any]]:
    """Repodata records already in the database, keyed by (subdir, filename).

    ``PackageVersion.info`` holds the record as repodata carried it, so a
    row that has one can be republished without touching object storage.
    Rows that stored something unusable fall back to their own columns,
    which carry everything repodata strictly requires.
    """
    result = await session.execute(
        select(PackageVersion, Package.name)
        .join(Package, PackageVersion.package_id == Package.id)
        .where(Package.channel_id == channel.id)
    )
    entries: dict[tuple[str, str], dict[str, Any]] = {}
    for row, package_name in result:
        info = dict(row.info or {})
        if not all(info.get(k) for k in ("name", "version", "build")):
            info = {
                "name": package_name,
                "version": row.version,
                "build": row.build,
                "build_number": row.build_number,
                "subdir": row.subdir,
                "depends": list(row.depends or []),
                "constrains": list(row.constrains or []),
            }
            if row.sha256:
                info["sha256"] = row.sha256
            if row.md5:
                info["md5"] = row.md5
            if row.package_timestamp:
                info["timestamp"] = int(row.package_timestamp.timestamp() * 1000)
        if row.size is not None:
            info["size"] = row.size
        entries[(row.subdir, row.filename)] = info
    return entries


async def _entry_from_archive(
    storage: Storage, key: str, filename: str, size: int
) -> dict[str, Any] | None:
    """Read one archive's repodata record, holding it on disk only as long as that takes.

    Both hashes and ``info/index.json`` come out of a single pass over a
    single temp file, which is unlinked before this returns — on the
    failure paths too. Nothing else in the channel is on disk while it
    exists, which is the whole point.

    Returns ``None`` for an archive that is not a readable package, and
    raises ``ArchiveFetchError`` when the bytes could not be read at all.
    The distinction decides whether the channel gets republished without
    this package: a file rattler rejects is not one any client could have
    installed, so leaving it out of the index is correct, whereas a
    storage error says nothing about the package and must not be allowed
    to quietly delist it.
    """
    suffix = ".conda" if filename.endswith(".conda") else ".tar.bz2"
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    try:
        sha = hashlib.sha256()
        md5 = hashlib.md5()
        written = 0
        try:
            with os.fdopen(fd, "wb") as tmp:
                async for chunk in storage.stream(key):
                    sha.update(chunk)
                    md5.update(chunk)
                    written += len(chunk)
                    tmp.write(chunk)
        except Exception as exc:
            raise ArchiveFetchError(f"could not read {key}: {exc}") from exc

        try:
            index = rattler.IndexJson.from_package_archive(Path(tmp_path))
        except Exception as exc:
            log.debug("reindex.archive_unparseable", key=key, error=str(exc))
            return None
        return repodata_entry(
            index=index,
            filename=filename,
            size=written or size,
            sha256=sha.hexdigest(),
            md5=md5.hexdigest(),
        )
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)


def repodata_entry(
    *,
    index: Any,
    filename: str,
    size: int,
    sha256: str | None,
    md5: str | None,
) -> dict[str, Any]:
    """The repodata.json record for one archive, from its ``info/index.json``.

    Field-for-field what rattler-index writes, so a channel indexed by
    either route reads back the same. Optional fields are omitted rather
    than emitted as null — clients tolerate absence, and a null
    ``timestamp`` is not the same thing as no timestamp.
    """
    entry: dict[str, Any] = {
        "name": _index_name(index),
        "version": str(index.version),
        "build": index.build,
        "build_number": int(index.build_number or 0),
        "subdir": index.subdir,
        "size": size,
        "depends": [str(d) for d in (index.depends or [])],
        "constrains": [str(c) for c in (index.constrains or [])],
    }
    if sha256:
        entry["sha256"] = sha256
    if md5:
        entry["md5"] = md5
    if index.timestamp is not None:
        entry["timestamp"] = int(index.timestamp.timestamp() * 1000)
    for attr in ("license", "license_family", "platform", "arch"):
        value = getattr(index, attr, None)
        if value:
            entry[attr] = str(value)
    track = getattr(index, "track_features", None)
    if track:
        entry["track_features"] = (
            track if isinstance(track, str) else " ".join(str(f) for f in track)
        )
    return entry


def _index_name(index: Any) -> str:
    """The bare name string out of a rattler ``PackageName``.

    ``str()`` on one returns its repr rather than the name; same accessor
    dance as in ``conda_server.api.channels``.
    """
    name = index.name
    for attr in ("source", "normalized"):
        value = getattr(name, attr, None)
        if value:
            return str(value)
    return str(name)


def build_repodata(subdir: str, entries: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Assemble a repodata.json document from per-filename records.

    The two sections are the format's way of separating archive types:
    ``.tar.bz2`` under ``packages``, ``.conda`` under ``packages.conda``.
    Clients that only understand the legacy format read the first and
    ignore the second.
    """
    legacy: dict[str, dict[str, Any]] = {}
    modern: dict[str, dict[str, Any]] = {}
    for filename, entry in sorted(entries.items()):
        target = modern if filename.endswith(".conda") else legacy
        target[filename] = entry
    return {
        "info": {"subdir": subdir},
        "packages": legacy,
        "packages.conda": modern,
        "repodata_version": 2,
    }


async def load_repodata_entries(
    storage: Storage, prefix: str, subdir: str
) -> dict[str, dict[str, Any]]:
    """The published records for one subdir, flattened by filename.

    Both sections collapse into one mapping because filenames already
    encode the archive type, and every caller wants to look a record up
    by filename rather than to know which half it came from;
    ``build_repodata`` splits them apart again on the way out.

    **Only a genuinely absent index reads as an empty channel.** Callers
    merge into what comes back and write the result, so "empty" is
    indistinguishable from "delete every package in this subdir". A
    subdir nobody has published to yet is the one case where that is
    also the truth; a read that failed, or an index that will not parse,
    is not, and both raise instead. The repair for those is a reindex,
    which rebuilds from the artifacts rather than from this file.
    """
    key = f"{prefix}/{subdir}/repodata.json"
    if await storage.head(key) is None:
        return {}

    raw = await storage.get(key)
    try:
        data = json.loads(raw)
    except ValueError as exc:
        log.error("repodata.unparseable", prefix=prefix, subdir=subdir)
        raise ValueError(f"{key} is not readable JSON; reindex the channel") from exc

    entries: dict[str, dict[str, Any]] = {}
    for section in ("packages", "packages.conda"):
        for filename, info in (data.get(section) or {}).items():
            entries[filename] = info
    return entries


async def upsert_version(
    session: AsyncSession,
    channel: Channel,
    subdir: str,
    filename: str,
    info: dict[str, Any],
) -> PackageVersion:
    """Insert or refresh the row for one archive from its repodata record.

    Shares ``_build_version`` / ``_apply_version`` with the reindex sync
    so a package landing by upload and the same package rediscovered by a
    later reindex produce identical rows — otherwise the first reindex
    after a publish would report spurious updates forever.
    """
    pkg = await _get_or_create_package(session, channel, info["name"])
    result = await session.execute(
        select(PackageVersion).where(
            PackageVersion.package_id == pkg.id,
            PackageVersion.subdir == subdir,
            PackageVersion.filename == filename,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = _build_version(pkg.id, subdir, filename, info)
        session.add(row)
    else:
        _apply_version(row, subdir, filename, info)
    await session.flush()
    return row


async def write_repodata(
    storage: Storage, prefix: str, subdir: str, entries: dict[str, dict[str, Any]]
) -> None:
    """Publish repodata.json and its zstd sibling for one subdir.

    Both, because clients ask for the compressed one first and fall back
    to the plain one — leaving a stale ``.zst`` beside a fresh ``.json``
    would serve the outdated index to precisely the clients quickest to
    ask for it.
    """
    body = json.dumps(build_repodata(subdir, entries), separators=(",", ":")).encode()
    base = f"{prefix}/{subdir}/repodata.json"
    await storage.put(base, body, content_type="application/json")
    await storage.put(
        f"{base}.zst",
        zstandard.ZstdCompressor().compress(body),
        content_type="application/zstd",
    )


async def invalidate_sharded_index(storage: Storage, prefix: str, subdir: str) -> None:
    """Drop the CEP-16 shard index once repodata.json has moved past it.

    rattler-index publishes ``repodata_shards.msgpack.zst`` beside
    repodata.json, and pixi/rattler ask for it *first*. Nothing outside
    rattler-index can update it, so a subdir whose records were just
    rewritten keeps handing the superseded hashes to precisely the
    clients quickest to ask — the same failure ``write_repodata`` writes
    both the plain and the zstd index to avoid, one file further along.

    Removing it is a downgrade, not a loss: a client that misses the
    shard index falls back to repodata.json, which is exact, and the next
    rattler-backed reindex republishes it. The per-name shards it pointed
    at are content-addressed and unreachable without it, so they are left
    where they are rather than deleted one by one.
    """
    key = f"{prefix}/{subdir}/repodata_shards.msgpack.zst"
    if await storage.head(key) is None:
        return
    with contextlib.suppress(FileNotFoundError):
        await storage.delete(key)


def _build_s3_credentials(settings: StorageSettings) -> rattler.index.S3Credentials | None:
    """Only pass credentials when the user has set them explicitly.

    When omitted, rattler falls back to the standard AWS credential chain
    (env vars, profile, instance metadata).

    ``addressing_style`` defaults to ``virtual-host`` in py-rattler, which
    builds URLs like ``https://<bucket>.<endpoint>/...``. That only works
    when the endpoint has a wildcard TLS cert covering the bucket
    subdomain — AWS and most managed S3 providers do, but self-hosted
    setups (Garage, MinIO, Ceph) usually serve a single endpoint host
    with a cert for that host only. Path-style (``<endpoint>/<bucket>/``)
    is the portable default; users with an AWS-style wildcard can
    override via config.
    """
    if not (settings.access_key_id and settings.secret_access_key):
        return None
    endpoint = settings.endpoint or "https://s3.amazonaws.com"
    return rattler.index.S3Credentials(
        endpoint_url=endpoint,
        region=settings.region or "us-east-1",
        access_key_id=settings.access_key_id,
        secret_access_key=settings.secret_access_key,
        addressing_style="path",
    )


async def _sync_db_from_repodata(
    session: AsyncSession,
    storage: Storage,
    channel: Channel,
) -> dict[str, int]:
    """Read each subdir's repodata.json from storage and upsert rows."""
    added = updated = removed = 0

    result = await session.execute(
        select(PackageVersion)
        .join(Package, PackageVersion.package_id == Package.id)
        .where(Package.channel_id == channel.id)
    )
    existing: dict[tuple[str, str], PackageVersion] = {
        (v.subdir, v.filename): v for v in result.scalars()
    }
    seen: set[tuple[str, str]] = set()
    prefix = channel.storage_prefix.strip("/")
    # Packages whose artifacts moved in this pass, and the specific rows
    # whose bytes were replaced. Which of them are worth opening cannot
    # be decided here: it depends on the package's full version list,
    # which is not complete until every subdir has been read.
    touched: set[int] = set()
    replaced: set[PackageVersion] = set()

    for subdir in SUBDIRS:
        key = f"{prefix}/{subdir}/repodata.json"
        try:
            raw = await storage.get(key)
        except Exception:
            continue

        data = json.loads(raw)
        # Both .tar.bz2 ("packages") and .conda ("packages.conda") sections.
        for section in ("packages", "packages.conda"):
            for filename, info in (data.get(section) or {}).items():
                seen.add((subdir, filename))
                pkg = await _get_or_create_package(session, channel, info["name"])

                existing_row = existing.get((subdir, filename))
                if existing_row is None:
                    row = _build_version(pkg.id, subdir, filename, info)
                    session.add(row)
                    touched.add(pkg.id)
                    added += 1
                elif _version_changed(existing_row, info):
                    if _should_recapture_about(existing_row, info):
                        replaced.add(existing_row)
                        touched.add(pkg.id)
                    _apply_version(existing_row, subdir, filename, info)
                    updated += 1

    for orphan_key, orphan_row in existing.items():
        if orphan_key not in seen:
            await session.delete(orphan_row)
            removed += 1

    await session.flush()
    await _capture_about(session, storage, prefix, touched, replaced)
    await session.flush()
    return {"added": added, "updated": updated, "removed": removed}


async def _get_or_create_package(session: AsyncSession, channel: Channel, name: str) -> Package:
    result = await session.execute(
        select(Package).where(Package.channel_id == channel.id, Package.name == name)
    )
    pkg = result.scalar_one_or_none()
    if pkg is None:
        pkg = Package(channel_id=channel.id, name=name)
        session.add(pkg)
        await session.flush()
    return pkg


def _build_version(
    package_id: int, subdir: str, filename: str, info: dict[str, Any]
) -> PackageVersion:
    ts = info.get("timestamp")
    return PackageVersion(
        package_id=package_id,
        version=str(info["version"]),
        build=str(info["build"]),
        build_number=int(info.get("build_number", 0) or 0),
        subdir=subdir,
        filename=filename,
        sha256=info.get("sha256"),
        md5=info.get("md5"),
        size=info.get("size"),
        depends=list(info.get("depends") or []),
        constrains=list(info.get("constrains") or []),
        package_timestamp=_timestamp_from_ms(ts) if ts else None,
        info=info,
    )


def _apply_version(row: PackageVersion, subdir: str, filename: str, info: dict[str, Any]) -> None:
    row.version = str(info["version"])
    row.build = str(info["build"])
    row.build_number = int(info.get("build_number", 0) or 0)
    row.subdir = subdir
    row.filename = filename
    row.sha256 = info.get("sha256")
    row.md5 = info.get("md5")
    row.size = info.get("size")
    row.depends = list(info.get("depends") or [])
    row.constrains = list(info.get("constrains") or [])
    ts = info.get("timestamp")
    row.package_timestamp = _timestamp_from_ms(ts) if ts else None
    row.info = info


def _version_changed(row: PackageVersion, info: dict[str, Any]) -> bool:
    return row.sha256 != info.get("sha256") or row.size != info.get("size")


def _should_recapture_about(row: PackageVersion, info: dict[str, Any]) -> bool:
    """Whether a *changed* row's about.json is worth re-reading.

    Two cases qualify, and one common one deliberately doesn't:

    * Never looked (``about_fetched_at`` is null) — includes every row
      that predates this column, so a channel picks its metadata up as
      artifacts churn without a dedicated pass.
    * The bytes were genuinely replaced: a known sha256 changed to a
      different known sha256, so the recipe (and its docs URL) may have
      moved.

    The excluded case is the row the import-from-upstream path just
    created inline. It already carries metadata read from the local copy
    but no sha256, so the first reindex after it lands always looks
    "changed" — re-opening the archive there would fetch the whole
    artifact back out of storage to learn what we already know.
    """
    if row.about_fetched_at is None:
        return True
    new_sha = info.get("sha256")
    return row.sha256 is not None and new_sha is not None and row.sha256 != new_sha


def apply_about(row: PackageVersion, about: PackageAbout) -> None:
    """Copy an extracted ``about.json`` onto a version row and stamp it.

    ``about_fetched_at`` is set even for an empty result — that is the
    difference between "this archive has no about.json" and "nobody has
    looked yet", and it is what lets the backfill command skip archives
    it has already opened instead of re-downloading them every run.
    """
    row.doc_url = about.doc_url
    row.home = about.home
    row.dev_url = about.dev_url
    row.summary = about.summary
    row.description = about.description
    row.about_fetched_at = datetime.now(UTC)


async def capture_about(
    storage: Storage,
    prefix: str,
    row: PackageVersion,
) -> bool:
    """Read one artifact's ``about.json`` from storage onto its row.

    Returns True when the row was stamped (whether or not any metadata
    was found), False when the archive could not be fetched at all —
    those are left unstamped deliberately, so a transient storage error
    is retried on the next pass rather than remembered as "this package
    has no metadata".

    Which of the two paths below runs is decided by the archive format,
    not by configuration, because it is the format that decides whether
    the metadata can be reached without moving the whole artifact.
    """
    key = f"{prefix}/{row.subdir}/{row.filename}"
    if row.filename.endswith(".conda"):
        return await _capture_about_ranged(storage, key, row)
    return await _capture_about_spooled(storage, key, row)


async def _capture_about_ranged(storage: Storage, key: str, row: PackageVersion) -> bool:
    """``.conda``: read the zip's tail and its info member, nothing else.

    No size cap applies here, and none is needed: the reads are the
    archive's tail window plus one member of a few KB, so the cost does
    not grow with the artifact. A cap would only mean declining metadata
    for large packages in exchange for saving nothing.
    """
    fetched = 0

    async def read_range(start: int, length: int) -> bytes:
        nonlocal fetched
        try:
            chunk = await storage.get_range(key, start=start, length=length)
        except Exception as exc:
            raise ArchiveFetchError(str(exc)) from exc
        fetched += len(chunk)
        return chunk

    try:
        meta = await storage.head(key)
    except Exception as exc:
        log.debug("about.fetch_failed", key=key, error=str(exc))
        return False
    if meta is None:
        log.debug("about.fetch_failed", key=key, error="object not found")
        return False

    try:
        about = await read_conda_about_ranged(read_range, meta.size)
    except ArchiveFetchError as exc:
        log.debug("about.fetch_failed", key=key, error=str(exc))
        return False

    log.debug("about.ranged_read", key=key, archive_bytes=meta.size, fetched_bytes=fetched)
    apply_about(row, about)
    return True


async def _capture_about_spooled(storage: Storage, key: str, row: PackageVersion) -> bool:
    """``.tar.bz2``: the whole object, via a temporary file.

    The legacy format is a single solid bz2 stream with no index, so the
    only route to ``info/about.json`` is decompressing from the start —
    there is nothing for a ranged read to seek to. That leaves the
    original approach, and with it the original cost, which is why
    ``MAX_ABOUT_ARCHIVE_BYTES`` still guards this path and only this one.

    Oversized archives are stamped rather than left pending: the cap is a
    policy decision, not a transient failure, and retrying would re-read
    the same object forever.
    """
    if row.size is not None and row.size > MAX_ABOUT_ARCHIVE_BYTES:
        log.debug("about.skipped_oversized", filename=row.filename, size=row.size)
        apply_about(row, PackageAbout.empty())
        return True

    fd, tmp_path = tempfile.mkstemp(suffix=".tar.bz2")
    try:
        try:
            with os.fdopen(fd, "wb") as tmp:
                async for chunk in storage.stream(key):
                    tmp.write(chunk)
        except Exception as exc:
            log.debug("about.fetch_failed", key=key, error=str(exc))
            return False

        apply_about(row, read_package_about(tmp_path))
        return True
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)


async def _capture_about(
    session: AsyncSession,
    storage: Storage,
    prefix: str,
    package_ids: set[int],
    replaced: set[PackageVersion],
) -> None:
    """Best-effort ``about.json`` capture for the versions that are shown.

    Two bounds, and they compose.

    The first is *which packages*: only those whose artifacts moved in
    this pass — something added, or bytes genuinely replaced. An untouched
    package is never looked at, so a steady-state reindex of an unchanged
    channel opens exactly zero archives.

    The second is *which of their versions*: only the newest, because
    that is the only one the package page can render. ``_about_source``
    in the packages API picks the newest version by conda ordering, so
    metadata captured for any older version is metadata nothing asks for.
    A package usually has several versions in a channel, and capturing
    every one of them multiplied this cost by exactly the factor of that
    redundancy.

    "Newest" is not fixed, which is why this runs against the package's
    whole version list rather than against the rows the loop above
    happened to touch: a rebuild of an older version can land *after* a
    newer one shipped, and it must not be mistaken for the newest. The
    flip side is handled by the same rule — when a genuinely new newest
    version appears, its package is touched, the list is recomputed, and
    it is captured on that pass.

    Rows are still stamped whether or not the archive had an
    ``about.json``, so nothing here is re-opened on the next pass.
    Versions that are not the newest are left uninspected; filling those
    in is what ``conda_server.backfill`` is for, and rows it fills are
    what keeps ``_about_source``'s older-version fallback meaningful.
    """
    for package_id in sorted(package_ids):
        result = await session.execute(
            select(PackageVersion).where(PackageVersion.package_id == package_id)
        )
        ordered = sort_versions(list(result.scalars()))
        for row, rank in zip(ordered, version_ranks(ordered), strict=True):
            # ``ordered`` is newest-first and ranks only ever increase, so
            # the first non-zero rank ends the newest version's artifacts.
            if rank != 0:
                break
            if row in replaced:
                row.about_fetched_at = None
            elif row.about_fetched_at is not None:
                continue
            await capture_about(storage, prefix, row)


def _timestamp_from_ms(ts: int | float) -> datetime:
    # Conda repodata timestamps are milliseconds since epoch.
    return datetime.fromtimestamp(float(ts) / 1000.0, tz=UTC)


async def repodata_payload(
    _session: AsyncSession, _channel: Channel, subdir: str
) -> dict[str, Any]:
    """Minimal empty repodata used as a fallback when no file exists in storage yet."""
    return {
        "info": {"subdir": subdir},
        "packages": {},
        "packages.conda": {},
        "repodata_version": 2,
    }
