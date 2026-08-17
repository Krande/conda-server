"""Mirror / proxy fetch helpers.

When a Channel has ``mirror_url`` set, requests for files not present in
local storage are resolved against the upstream channel. Packages are cached
forever (content-addressed by filename). Repodata artifacts are cached for
``channel.mirror_cache_seconds`` seconds.

HTTP fetches use a module-level ``httpx.AsyncClient`` so connection pooling
is shared across requests. The client is overridable via ``set_http_client``
for tests.
"""

from __future__ import annotations

import time

import httpx

from conda_server.logging import get_logger
from conda_server.metrics import (
    MIRROR_CACHE_HITS,
    MIRROR_UPSTREAM_BYTES,
    MIRROR_UPSTREAM_FETCHES,
)
from conda_server.models import Channel
from conda_server.storage import ObstoreStorage, Storage


def _package_kind(filename: str) -> str:
    """Classify a package-path filename for metric labels."""
    if filename.endswith((".conda", ".tar.bz2")):
        return "package"
    if filename.endswith(".msgpack.zst"):
        return "shard"
    return "other"


# Chunk size for the fetch → storage stream. 1 MiB keeps memory bounded
# during conda-forge-style 100+ MB repodata pulls while still amortizing
# syscall overhead on the write side.
_STREAM_CHUNK = 1 * 1024 * 1024

log = get_logger(__name__)

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(30.0, read=120.0),
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        )
    return _client


def set_http_client(client: httpx.AsyncClient | None) -> None:
    """Override the module-level client. Tests inject an httpx.MockTransport here."""
    global _client
    _client = client


async def shutdown() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _mirror_key(channel: Channel, subdir: str, filename: str) -> str:
    return f"{channel.storage_prefix.strip('/')}/{subdir}/{filename}"


def _mirror_url(channel: Channel, subdir: str, filename: str) -> str | None:
    if not channel.mirror_url:
        return None
    return f"{channel.mirror_url.rstrip('/')}/{subdir}/{filename}"


async def head_upstream(url: str) -> httpx.Response | None:
    """Send a HEAD to an upstream URL using the shared httpx client.

    Returns the response on any <500 status so callers can forward
    200/404 verbatim; returns None on transport errors or 5xx so the
    caller can decide whether to 404 or fall back to a cached copy.
    """
    try:
        response = await _get_client().head(url, follow_redirects=True)
    except httpx.HTTPError as exc:
        log.warning("mirror.head_error", url=url, error=str(exc))
        return None
    if response.status_code >= 500:
        log.warning("mirror.head_status", url=url, status=response.status_code)
        return None
    return response


def _upstream_headers_for(filename: str) -> tuple[str | None, str | None]:
    """Pick Content-Type / Content-Disposition to persist on the cached object.

    Anything a client would download to disk gets ``attachment; filename=...``
    so the browser uses the proper name — otherwise Garage returns the
    object with no headers and Chrome sniffs .conda's ZIP magic to rename
    it .zip. Repodata artifacts are consumed inline (parsed in memory),
    so they get a content-type but no attachment header.
    """
    if filename.endswith(".conda"):
        return "application/x-conda", f'attachment; filename="{filename}"'
    if filename.endswith(".tar.bz2"):
        return "application/x-tar", f'attachment; filename="{filename}"'
    if filename.endswith(".msgpack.zst"):
        return "application/x-msgpack", None
    if filename.endswith(".json.zst"):
        return "application/zstd", None
    if filename.endswith(".json"):
        return "application/json", None
    return None, None


async def _stream_upstream_to_storage(
    url: str, storage: Storage, key: str, filename: str
) -> int | None:
    """Pipe an upstream response straight into object storage.

    Returns the number of bytes written, or None on 4xx/5xx or transport error.
    Memory usage is bounded to ``_STREAM_CHUNK`` bytes per in-flight request;
    conda-forge's ~150 MB repodata no longer needs to fit in the pod all at once.
    """
    client = _get_client()
    content_type, content_disposition = _upstream_headers_for(filename)
    try:
        async with client.stream("GET", url) as response:
            if response.status_code == 404:
                return None
            if response.status_code >= 400:
                log.warning("mirror.fetch_status", url=url, status=response.status_code)
                return None

            async def _chunks():
                async for chunk in response.aiter_bytes(_STREAM_CHUNK):
                    yield chunk

            # put_stream keeps peak memory at _STREAM_CHUNK regardless of
            # backend and attaches the Content-Type/Disposition so Garage
            # (S3) returns them on every GET, including presigned URLs.
            if isinstance(storage, ObstoreStorage):
                return await storage.put_stream(
                    key,
                    _chunks(),
                    content_type=content_type,
                    content_disposition=content_disposition,
                )
            # Test fakes / in-memory Storage: buffered path.
            buffer = bytearray()
            total = 0
            async for chunk in response.aiter_bytes(_STREAM_CHUNK):
                buffer.extend(chunk)
                total += len(chunk)
            await storage.put(key, bytes(buffer))
            return total
    except httpx.HTTPError as exc:
        log.warning("mirror.fetch_error", url=url, error=str(exc))
        return None


async def fetch_package(storage: Storage, channel: Channel, subdir: str, filename: str) -> bool:
    """Fetch a package from upstream, caching forever. Returns True on success.

    The bytes never land in memory as a single blob — they're streamed from
    httpx straight into obstore. The caller is expected to serve them via
    ``storage.stream`` (local backend) or a presigned redirect (S3/Azure).
    """
    url = _mirror_url(channel, subdir, filename)
    if url is None:
        return False
    key = _mirror_key(channel, subdir, filename)
    kind = _package_kind(filename)
    written = await _stream_upstream_to_storage(url, storage, key, filename)
    if written is None:
        MIRROR_UPSTREAM_FETCHES.labels(channel=channel.name, kind=kind, result="failure").inc()
        return False
    MIRROR_UPSTREAM_FETCHES.labels(channel=channel.name, kind=kind, result="success").inc()
    MIRROR_UPSTREAM_BYTES.labels(channel=channel.name, kind=kind).inc(written)
    log.info("mirror.package_cached", channel=channel.name, key=key, size=written)
    return True


async def fetch_repodata(storage: Storage, channel: Channel, subdir: str, filename: str) -> bool:
    """Ensure a fresh-enough repodata artifact exists in storage.

    Returns True when a usable cached object is available after the call
    (either already within TTL, just refreshed, or a stale fallback worth
    serving). Returns False only when upstream gave us nothing AND there's
    no cached copy.

    Cache freshness is keyed on the object's ``last_modified`` timestamp.
    When the cached file is older than ``channel.mirror_cache_seconds``, we
    refetch upstream. On upstream failure with a stale-but-present cached
    copy, we fall back to the stale copy (better to serve stale than to 404).
    """
    url = _mirror_url(channel, subdir, filename)
    if url is None:
        return False
    key = _mirror_key(channel, subdir, filename)

    meta = await storage.head(key)
    if meta is not None and meta.last_modified is not None:
        age = time.time() - meta.last_modified
        if age < max(channel.mirror_cache_seconds, 0):
            MIRROR_CACHE_HITS.labels(channel=channel.name, kind="repodata").inc()
            return True

    written = await _stream_upstream_to_storage(url, storage, key, filename)
    if written is None:
        if meta is not None:
            MIRROR_UPSTREAM_FETCHES.labels(
                channel=channel.name, kind="repodata", result="stale_fallback"
            ).inc()
            log.info("mirror.repodata_stale_fallback", channel=channel.name, key=key)
            return True
        MIRROR_UPSTREAM_FETCHES.labels(
            channel=channel.name, kind="repodata", result="failure"
        ).inc()
        return False

    MIRROR_UPSTREAM_FETCHES.labels(channel=channel.name, kind="repodata", result="success").inc()
    MIRROR_UPSTREAM_BYTES.labels(channel=channel.name, kind="repodata").inc(written)
    log.info("mirror.repodata_refreshed", channel=channel.name, key=key, size=written)
    return True
