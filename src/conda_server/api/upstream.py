"""Upstream-channel browsing for the import-from-upstream flow.

Two endpoints, both authenticated (we don't want to expose an open
conda-forge proxy):

- ``GET /api/upstream/search`` — substring match on package names from
  the upstream's CEP-16 shards index. Returns the matching names and a
  signal whether each is available locally.
- ``GET /api/upstream/versions`` — fetch a single name's shard and
  enumerate its versions, ready to be passed to ``import``.

Why two endpoints rather than one fat search-with-versions: a
substring match for "py" against conda-forge linux-64 hits thousands
of shards. Listing names is cheap (one ~1 MB shards index decode);
listing every version of every match would mean pulling thousands of
small shards. Splitting lets the UI show a compact first page and pull
detail only for whatever the operator actually wants to import.

Sharded repodata only — no fallback to the monolithic 400 MB
``repodata.json``. CEP-16 has been the canonical conda-forge format
for a while, and a non-sharded upstream is an explicit out-of-scope
case for now (we'd add ijson stream-parsing if a real user hits it).
"""

from __future__ import annotations

import asyncio
import time
from typing import Annotated, Any

import httpx
import msgpack
import zstandard
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select

from conda_server.auth import current_user
from conda_server.db import SessionDep
from conda_server.logging import get_logger
from conda_server.mirror import _get_client
from conda_server.models import Channel, Package, PackageVersion, User

router = APIRouter(prefix="/upstream", tags=["upstream"])
log = get_logger(__name__)

VALID_SUBDIRS = {
    "noarch",
    "linux-64",
    "linux-aarch64",
    "linux-ppc64le",
    "osx-64",
    "osx-arm64",
    "win-64",
}

_MAX_SEARCH_RESULTS = 50
_MAX_VERSIONS_PER_PACKAGE = 200
_SHARDS_INDEX_TTL_SECONDS = 600  # 10 min — same ballpark as our mirror cache.


class UpstreamPackageHit(BaseModel):
    """One upstream package name + a signal whether we already have it locally."""

    name: str
    # Names of locally-visible non-mirror channels that already index
    # this package (any version). Empty list = not yet imported anywhere.
    in_channels: list[str] = []


class UpstreamSearchResult(BaseModel):
    upstream_url: str
    subdir: str
    matched: int
    truncated: bool
    packages: list[UpstreamPackageHit]


class UpstreamVersion(BaseModel):
    name: str
    version: str
    build: str
    build_number: int = 0
    subdir: str
    filename: str
    size: int | None = None
    sha256: str | None = None
    md5: str | None = None
    depends: list[str] = []
    constrains: list[str] = []
    # True when this exact filename already lives in the channel the
    # operator is importing into. Lets the UI grey out duplicates.
    in_target_channel: bool = False


class UpstreamVersionsResult(BaseModel):
    upstream_url: str
    subdir: str
    name: str
    versions: list[UpstreamVersion]
    truncated: bool


# In-process TTL cache for the shards index. Keyed by (url, subdir);
# each pod has its own cache. Crude but fine for the cardinality of
# upstream channels an operator browses (typically one).
_shards_cache: dict[tuple[str, str], tuple[float, dict[bytes, bytes]]] = {}
_shards_cache_lock = asyncio.Lock()


def _normalize_url(url: str) -> str:
    return url.strip().rstrip("/")


def _shards_index_url(upstream_url: str, subdir: str) -> str:
    return f"{upstream_url}/{subdir}/repodata_shards.msgpack.zst"


def _shard_url(upstream_url: str, subdir: str, sha_hex: str) -> str:
    return f"{upstream_url}/{subdir}/{sha_hex}.msgpack.zst"


def _decode_msgpack_zst(blob: bytes) -> Any:
    """Decompress + msgpack-decode a CEP-16 shard or shards index."""
    raw = zstandard.ZstdDecompressor().decompress(blob)
    return msgpack.unpackb(raw, raw=True)


async def _fetch_shards_index(upstream_url: str, subdir: str) -> dict[bytes, bytes]:
    """Cached fetch of an upstream's CEP-16 shards index.

    Returns the ``shards`` map (package-name bytes → 32-byte sha hash).
    Other fields in the index file aren't needed for search. Cache TTL
    bounds drift; on miss we fetch fresh.
    """
    key = (upstream_url, subdir)
    now = time.monotonic()
    async with _shards_cache_lock:
        cached = _shards_cache.get(key)
        if cached is not None and (now - cached[0]) < _SHARDS_INDEX_TTL_SECONDS:
            return cached[1]

    url = _shards_index_url(upstream_url, subdir)
    client = _get_client()
    try:
        resp = await client.get(url)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"upstream fetch failed: {exc}",
        ) from exc
    if resp.status_code == 404:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "upstream doesn't expose CEP-16 sharded repodata for this subdir "
                f"({url}); only sharded upstreams are supported for now"
            ),
        )
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"upstream returned {resp.status_code} for shards index",
        )

    decoded = _decode_msgpack_zst(resp.content)
    shards = decoded.get(b"shards") if isinstance(decoded, dict) else None
    if not isinstance(shards, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="upstream shards index is malformed",
        )

    async with _shards_cache_lock:
        _shards_cache[key] = (now, shards)
    return shards


async def _fetch_shard(upstream_url: str, subdir: str, sha_hex: str) -> dict[str, Any] | None:
    """Fetch + decode one per-name shard. Returns None on 404."""
    url = _shard_url(upstream_url, subdir, sha_hex)
    client = _get_client()
    try:
        resp = await client.get(url)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"upstream fetch failed: {exc}",
        ) from exc
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"upstream returned {resp.status_code} for shard",
        )
    return _decode_msgpack_zst(resp.content)


@router.get("/search", response_model=UpstreamSearchResult)
async def upstream_search(
    session: SessionDep,
    user: Annotated[User, Depends(current_user)],
    url: str = Query(
        ..., description="Upstream channel URL, e.g. https://conda.anaconda.org/conda-forge"
    ),
    subdir: str = Query(..., description="Subdir to search, e.g. linux-64"),
    name: str = Query(
        ..., min_length=1, max_length=128, description="Substring to match against package names"
    ),
) -> UpstreamSearchResult:
    """Substring-match upstream package names from the CEP-16 shards index."""
    _ = user  # auth check via the dep
    if subdir not in VALID_SUBDIRS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid subdir")
    upstream_url = _normalize_url(url)
    if not upstream_url.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="upstream url must be http(s)://",
        )

    shards = await _fetch_shards_index(upstream_url, subdir)
    needle = name.strip().lower().encode("utf-8")

    matched_names: list[str] = []
    total_matches = 0
    for shard_name in shards:
        if needle in shard_name.lower():
            total_matches += 1
            if len(matched_names) < _MAX_SEARCH_RESULTS:
                matched_names.append(shard_name.decode("utf-8", errors="replace"))

    # Cross-reference with our local non-mirror channels so the UI can
    # signal duplicates ("already in: my-channel, other-channel").
    in_channels_by_name: dict[str, list[str]] = {n: [] for n in matched_names}
    if matched_names:
        result = await session.execute(
            select(Package.name, Channel.name.label("channel_name"))
            .join(Channel, Channel.id == Package.channel_id)
            .where(
                Package.name.in_(matched_names),
                Channel.mirror_url.is_(None),
            )
        )
        for pkg_name, channel_name in result:
            if pkg_name in in_channels_by_name:
                in_channels_by_name[pkg_name].append(channel_name)

    return UpstreamSearchResult(
        upstream_url=upstream_url,
        subdir=subdir,
        matched=total_matches,
        truncated=total_matches > len(matched_names),
        packages=[
            UpstreamPackageHit(name=n, in_channels=sorted(set(in_channels_by_name[n])))
            for n in matched_names
        ],
    )


@router.get("/versions", response_model=UpstreamVersionsResult)
async def upstream_versions(
    session: SessionDep,
    user: Annotated[User, Depends(current_user)],
    url: str = Query(...),
    subdir: str = Query(...),
    name: str = Query(..., min_length=1, max_length=128),
    target_channel: str | None = Query(
        None,
        description="Channel name the import is targeting; sets in_target_channel on each result.",
    ),
) -> UpstreamVersionsResult:
    """Enumerate every published version + build of a single upstream package."""
    _ = user
    if subdir not in VALID_SUBDIRS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid subdir")
    upstream_url = _normalize_url(url)

    shards = await _fetch_shards_index(upstream_url, subdir)
    sha_bytes = shards.get(name.encode("utf-8"))
    if sha_bytes is None:
        return UpstreamVersionsResult(
            upstream_url=upstream_url,
            subdir=subdir,
            name=name,
            versions=[],
            truncated=False,
        )
    shard = await _fetch_shard(upstream_url, subdir, sha_bytes.hex())
    if shard is None:
        return UpstreamVersionsResult(
            upstream_url=upstream_url,
            subdir=subdir,
            name=name,
            versions=[],
            truncated=False,
        )

    # Shard contains both old-style ".tar.bz2" entries under "packages"
    # and modern ".conda" entries under "packages.conda". Surface both.
    out: list[UpstreamVersion] = []
    for section_key in (b"packages.conda", b"packages"):
        section = shard.get(section_key) or {}
        for filename_bytes, info in section.items():
            if not isinstance(info, dict):
                continue
            out.append(
                UpstreamVersion(
                    name=_str(info.get(b"name")) or name,
                    version=_str(info.get(b"version")) or "",
                    build=_str(info.get(b"build")) or "",
                    build_number=int(info.get(b"build_number") or 0),
                    subdir=_str(info.get(b"subdir")) or subdir,
                    filename=filename_bytes.decode("utf-8", errors="replace"),
                    size=_int_or_none(info.get(b"size")),
                    sha256=_str_or_none(info.get(b"sha256")),
                    md5=_str_or_none(info.get(b"md5")),
                    depends=_str_list(info.get(b"depends")),
                    constrains=_str_list(info.get(b"constrains")),
                )
            )
    out.sort(
        key=lambda v: (v.version, v.build_number, v.build),
        reverse=True,
    )
    truncated = len(out) > _MAX_VERSIONS_PER_PACKAGE
    out = out[:_MAX_VERSIONS_PER_PACKAGE]

    if target_channel and out:
        existing = await session.execute(
            select(PackageVersion.filename, PackageVersion.subdir)
            .join(Package, Package.id == PackageVersion.package_id)
            .join(Channel, Channel.id == Package.channel_id)
            .where(Channel.name == target_channel, Package.name == name)
        )
        owned = {(s, f) for f, s in existing}
        for v in out:
            if (v.subdir, v.filename) in owned:
                v.in_target_channel = True

    return UpstreamVersionsResult(
        upstream_url=upstream_url,
        subdir=subdir,
        name=name,
        versions=out,
        truncated=truncated,
    )


def _str(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    return str(v)


def _str_or_none(v: Any) -> str | None:
    s = _str(v)
    return s if s else None


def _int_or_none(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _str_list(v: Any) -> list[str]:
    if not isinstance(v, list):
        return []
    return [_str(x) or "" for x in v]
