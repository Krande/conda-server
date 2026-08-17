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
"""

from __future__ import annotations

import dataclasses
import json
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
from conda_server.storage import Storage

log = get_logger(__name__)


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
                    session.add(_build_version(pkg.id, subdir, filename, info))
                    added += 1
                elif _version_changed(existing_row, info):
                    _apply_version(existing_row, subdir, filename, info)
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
