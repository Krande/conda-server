from __future__ import annotations

import json
import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from starlette.convertors import Convertor, register_url_convertor

from conda_server.auth import current_user_optional, visible_channel_or_404
from conda_server.config import get_settings
from conda_server.db import SessionDep
from conda_server.indexer import repodata_payload
from conda_server.metrics import MIRROR_CACHE_HITS
from conda_server.mirror import fetch_package, fetch_repodata, head_upstream
from conda_server.models import Channel, Package, PackageVersion, User
from conda_server.storage import get_storage

router = APIRouter(tags=["repodata"])

VALID_SUBDIRS = {
    "noarch",
    "linux-64",
    "linux-aarch64",
    "linux-ppc64le",
    "osx-64",
    "osx-arm64",
    "win-64",
}


class _SubdirConvertor(Convertor):
    """Route-level guard so only real conda subdirs match the repodata URL.

    Without this, deep SPA routes like ``/channels/conda-forge/packages/foo``
    match the catch-all ``/{channel_name}/{subdir}/{filename:path}`` pattern,
    and the handler 404s with ``unknown subdir`` before the SPA fallback
    has a chance to serve index.html. Limiting the pattern to the enum
    keeps the conda-client URL scheme intact while freeing every other
    second-segment value for the SPA.
    """

    regex = "noarch|linux-64|linux-aarch64|linux-ppc64le|osx-64|osx-arm64|win-64"

    def convert(self, value: str) -> str:
        return value

    def to_string(self, value: str) -> str:
        return value


register_url_convertor("subdir", _SubdirConvertor())

REPODATA_FILES = {
    "repodata.json": "application/json",
    "repodata.json.zst": "application/zstd",
    "current_repodata.json": "application/json",
    "current_repodata.json.zst": "application/zstd",
    # CEP-16 sharded index. Small (~few MB) and changes whenever upstream
    # regenerates, so treat it like other repodata artifacts with TTL.
    "repodata_shards.msgpack.zst": "application/x-msgpack",
}


_SHARD_RE = re.compile(r"^shards/[0-9a-f]{64}\.msgpack\.zst$")


def _is_shard(filename: str) -> bool:
    """CEP-16 per-name shard — `shards/<sha256-hex>.msgpack.zst` under the subdir.

    The `repodata_shards.msgpack.zst` index references these by sha256
    hash and the convention (CEP-16, what rattler-index writes and what
    pixi/rattler requests) is to keep them in a `shards/` subfolder
    alongside `repodata.json`. Content-addressed, so cache forever.
    """
    return _SHARD_RE.match(filename) is not None


@router.api_route(
    "/{channel_name}/{subdir:subdir}/{filename:path}",
    methods=["GET", "HEAD"],
)
async def get_channel_file(
    request: Request,
    channel_name: str,
    subdir: str,
    filename: str,
    session: SessionDep,
    user: Annotated[User | None, Depends(current_user_optional)],
) -> Response:
    """Serve repodata artifacts, sharded index files, and package archives.

    HEAD is supported: pixi/rattler issue HEAD to probe for existence before
    downloading. Without it the probe falls back to the client's content-
    addressed local cache and the bytes never come through this server.
    The HEAD path avoids triggering an upstream cache-write — mirror
    channels forward the HEAD upstream instead.

    """
    # Subdir validity is enforced at route level by the :subdir convertor,
    # so this handler only sees real conda subdirs.
    channel = await visible_channel_or_404(session, channel_name, user)

    head_only = request.method == "HEAD"

    if filename in REPODATA_FILES:
        return await _serve_repodata(channel, subdir, filename, session, head_only)
    if _is_shard(filename):
        return await _serve_shard(channel, subdir, filename, head_only)
    if filename.endswith((".conda", ".tar.bz2")):
        return await _serve_package(channel, subdir, filename, session, head_only)

    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="file not found")


async def _serve_repodata(
    channel: Channel,
    subdir: str,
    filename: str,
    session: SessionDep,
    head_only: bool = False,
) -> Response:
    storage = get_storage()
    key = f"{channel.storage_prefix.strip('/')}/{subdir}/{filename}"

    if head_only:
        # Pure existence probe — never trigger an upstream fetch.
        meta = await storage.head(key)
        if meta is not None:
            return Response(
                status_code=status.HTTP_200_OK,
                media_type=REPODATA_FILES[filename],
                headers={"Content-Length": str(meta.size)},
            )
        # repodata.json always has a DB-rendered empty fallback; mirror
        # channels are assumed to have upstream coverage.
        if channel.mirror_url or filename == "repodata.json":
            return Response(status_code=status.HTTP_200_OK, media_type=REPODATA_FILES[filename])
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    # Mirror channels refresh through the TTL-cached proxy. The helper
    # returns True when a cached object is available; we then stream the
    # bytes back out — the full 100+ MB repodata never fits in the Python
    # process at once.
    if channel.mirror_url:
        if not await fetch_repodata(storage, channel, subdir, filename):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="repodata artifact not found upstream",
            )
        meta = await storage.head(key)
        etag = _repodata_etag(channel, subdir, filename, meta.size if meta else 0)
        return StreamingResponse(
            storage.stream(key),
            media_type=REPODATA_FILES[filename],
            headers={"Cache-Control": "public, max-age=60", "ETag": etag},
        )

    # Non-mirror path: try local storage, fall back to a DB-rendered empty
    # repodata.json for bootstrap-style channels.
    meta = await storage.head(key)
    if meta is not None:
        etag = _repodata_etag(channel, subdir, filename, meta.size)
        return StreamingResponse(
            storage.stream(key),
            media_type=REPODATA_FILES[filename],
            headers={"Cache-Control": "public, max-age=60", "ETag": etag},
        )

    if filename != "repodata.json":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="repodata artifact not found; run reindex or configure mirror",
        )
    payload = await repodata_payload(session, channel, subdir)
    body = json.dumps(payload, separators=(",", ":")).encode()
    return Response(
        content=body,
        media_type=REPODATA_FILES[filename],
        headers={
            "Cache-Control": "public, max-age=60",
            "ETag": _repodata_etag(channel, subdir, filename, len(body)),
        },
    )


def _repodata_etag(channel: Channel, subdir: str, filename: str, size: int) -> str:
    ts = int(channel.repodata_updated_at.timestamp()) if channel.repodata_updated_at else 0
    return f'W/"{channel.id}-{subdir}-{filename}-{ts}-{size}"'


async def _serve_shard(
    channel: Channel,
    subdir: str,
    filename: str,
    head_only: bool = False,
) -> Response:
    """Per-name CEP-16 shard — content-addressed, cache forever.

    Lives at ``<subdir>/shards/<sha256-hex>.msgpack.zst`` both upstream
    and locally (the CEP-16 layout — what rattler-index writes and what
    pixi/rattler requests). 302 to a presigned URL on cache hit; mirror
    channels fetch upstream first, storing content-type +
    content-disposition on the object so the eventual redirect serves
    the right headers.
    """
    storage = get_storage()
    key = f"{channel.storage_prefix.strip('/')}/{subdir}/{filename}"
    settings = get_settings()

    if head_only:
        return await _head_mirror_content(channel, subdir, filename, storage, key)

    head = await storage.head(key)
    if head is None and channel.mirror_url:
        if not await fetch_package(storage, channel, subdir, filename):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="shard not found")
        head = await storage.head(key)
    elif head is not None and channel.mirror_url:
        MIRROR_CACHE_HITS.labels(channel=channel.name, kind="shard").inc()

    if head is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="shard not found")

    try:
        url = await storage.presign_get(key, expires_in=settings.storage.presign_ttl_seconds)
        return Response(status_code=status.HTTP_302_FOUND, headers={"Location": url})
    except NotImplementedError:
        return StreamingResponse(
            storage.stream(key),
            media_type="application/x-msgpack",
            headers={"Content-Length": str(head.size)},
        )


async def _serve_package(
    channel: Channel,
    subdir: str,
    filename: str,
    session: SessionDep,
    head_only: bool = False,
) -> Response:
    storage = get_storage()
    key = f"{channel.storage_prefix.strip('/')}/{subdir}/{filename}"
    settings = get_settings()

    # For non-mirror channels we enforce an ACL by filename via the DB. Mirror
    # channels trust upstream — any filename upstream serves is fair game —
    # so the DB lookup is skipped and we go straight to fetch-if-missing.
    if not channel.mirror_url:
        result = await session.execute(
            select(PackageVersion)
            .join(Package, Package.id == PackageVersion.package_id)
            .where(
                Package.channel_id == channel.id,
                PackageVersion.subdir == subdir,
                PackageVersion.filename == filename,
            )
        )
        if result.scalar_one_or_none() is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="package not found")

    if head_only:
        return await _head_mirror_content(channel, subdir, filename, storage, key)

    head = await storage.head(key)
    if head is None and channel.mirror_url:
        # Cache miss: pull from upstream and let head() discover the size.
        if not await fetch_package(storage, channel, subdir, filename):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="package not found")
        head = await storage.head(key)
    elif head is not None and channel.mirror_url:
        MIRROR_CACHE_HITS.labels(channel=channel.name, kind="package").inc()

    if head is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="package not found")

    try:
        url = await storage.presign_get(key, expires_in=settings.storage.presign_ttl_seconds)
        return Response(status_code=status.HTTP_302_FOUND, headers={"Location": url})
    except NotImplementedError:
        # Local-backend fallback: stream the bytes through with an explicit
        # Content-Disposition so the filename is preserved. For S3 backends
        # the same headers come from the object's own attributes set at
        # PUT time — see storage.put_stream.
        return StreamingResponse(
            storage.stream(key),
            media_type="application/octet-stream",
            headers={
                "Content-Length": str(head.size),
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )


async def _head_mirror_content(
    channel: Channel, subdir: str, filename: str, storage, key: str
) -> Response:
    """HEAD for .conda / .tar.bz2 / shards URLs — existence probe, no bytes.

    If locally cached, answer from storage metadata. For mirror channels that
    haven't cached this file yet, forward the HEAD upstream so probing is
    cheap and doesn't cause a cache-write every time pixi/rattler checks a
    solve result.
    """
    meta = await storage.head(key)
    if meta is not None:
        return Response(
            status_code=status.HTTP_200_OK,
            media_type="application/octet-stream",
            headers={"Content-Length": str(meta.size)},
        )

    if channel.mirror_url:
        url = f"{channel.mirror_url.rstrip('/')}/{subdir}/{filename}"
        upstream = await head_upstream(url)
        if upstream is not None and upstream.status_code == 200:
            content_length = upstream.headers.get("content-length", "0")
            return Response(
                status_code=status.HTTP_200_OK,
                media_type="application/octet-stream",
                headers={"Content-Length": content_length},
            )

    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
