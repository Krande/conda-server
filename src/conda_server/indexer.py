"""Channel reindexing — scans object storage, updates repodata.json, syncs DB.

The heavy lifting (parsing .conda archives, generating repodata.json + zst) is
delegated to ``rattler.index``. For the S3 backend we use ``index_s3`` which
operates directly against the bucket. For the local backend we point
``index_fs`` at the local path — no copy required. For Azure / GCS we fall
back to mirroring the channel into a temp directory, indexing there, and
uploading the generated repodata artifacts back.

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
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import rattler.index
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

REPODATA_ARTIFACTS = (
    "repodata.json",
    "repodata.json.zst",
    "current_repodata.json",
    "current_repodata.json.zst",
)


@dataclass
class IndexResult:
    channel: str
    added: int
    updated: int
    removed: int


async def reindex_channel(
    session: AsyncSession,
    storage: Storage,
    channel: Channel,
) -> IndexResult:
    """Run rattler-index over the channel's storage prefix and sync the DB."""
    settings = get_settings().storage
    log.info("reindex.start", channel=channel.name, backend=settings.backend)

    if settings.backend == "local":
        await _reindex_local(settings, channel)
    elif settings.backend == "s3":
        await _reindex_s3(settings, channel)
    else:
        await _reindex_via_temp(storage, channel)

    stats = await _sync_db_from_repodata(session, storage, channel)
    channel.repodata_updated_at = datetime.now(UTC)

    log.info("reindex.done", channel=channel.name, **stats)
    return IndexResult(channel=channel.name, **stats)


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


async def _reindex_via_temp(storage: Storage, channel: Channel) -> None:
    """Generic fallback: mirror the channel to a temp dir, index, upload back."""
    prefix = channel.storage_prefix.strip("/")
    with tempfile.TemporaryDirectory() as tmp:
        channel_dir = Path(tmp) / prefix.split("/")[-1]
        (channel_dir / "noarch").mkdir(parents=True, exist_ok=True)

        async for meta in storage.list(f"{prefix}/"):
            if not meta.key.endswith(PACKAGE_SUFFIXES):
                continue
            rel = meta.key[len(prefix) :].lstrip("/")
            dest = channel_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(await storage.get(meta.key))

        await rattler.index.index_fs(channel_dir)

        for artifact in channel_dir.rglob("*"):
            if not artifact.is_file():
                continue
            if artifact.name not in REPODATA_ARTIFACTS and artifact.name != "shards.msgpack.zst":
                continue
            rel = artifact.relative_to(channel_dir).as_posix()
            key = f"{prefix}/{rel}"
            await storage.put(key, artifact.read_bytes())


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
                    await _capture_about(storage, prefix, row)
                    added += 1
                elif _version_changed(existing_row, info):
                    recapture = _should_recapture_about(existing_row, info)
                    _apply_version(existing_row, subdir, filename, info)
                    if recapture:
                        existing_row.about_fetched_at = None
                        await _capture_about(storage, prefix, existing_row)
                    updated += 1

    for orphan_key, orphan_row in existing.items():
        if orphan_key not in seen:
            await session.delete(orphan_row)
            removed += 1

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
    created inline. It already carries metadata read from the /tmp copy
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


async def _capture_about(storage: Storage, prefix: str, row: PackageVersion) -> None:
    """Best-effort ``about.json`` capture during a reindex.

    Deliberately only reached for rows the sync just *added* or whose
    bytes just *changed*. An untouched row is never re-opened, so a
    steady-state reindex of an unchanged channel still downloads exactly
    zero archives — the cost added here is one archive read per newly
    indexed artifact, not one per artifact in the channel. That also
    means existing rows stay blank until they are re-uploaded or the
    ``conda-server backfill-about`` command is run against the channel.
    """
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
