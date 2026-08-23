from __future__ import annotations

import asyncio
import contextlib
import datetime as _dt
import os
import pathlib
import tempfile
from datetime import datetime
from typing import Annotated, Any

import httpx
import rattler
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    UploadFile,
    status,
)
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from conda_server import audit
from conda_server.auth import (
    AccessLevel,
    channel_access_level,
    current_user,
    current_user_optional,
    require_admin,
    require_channel_owner,
    require_channel_writer,
    visible_channel_or_404,
    visible_channels_stmt,
)
from conda_server.backfill import (
    DEFAULT_CONCURRENCY,
    BackfillStats,
    backfill_about_batch,
    count_pending,
)
from conda_server.config import get_settings
from conda_server.db import SessionDep, get_sessionmaker
from conda_server.indexer import apply_about, reindex_channel
from conda_server.logging import get_logger
from conda_server.metrics import (
    PACKAGE_DELETES,
    REINDEX_DURATION,
    REINDEX_RUNS,
    UPLOAD_BYTES,
    UPLOADS_TOTAL,
)
from conda_server.mirror import _get_client as _shared_http_client
from conda_server.mirror_listing import parse_conda_filename
from conda_server.models import (
    Channel,
    ChannelMember,
    ImportJob,
    MaintenanceJob,
    Package,
    PackageVersion,
    User,
)
from conda_server.package_about import read_package_about
from conda_server.storage import get_storage

router = APIRouter(prefix="/channels", tags=["channels"])
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

# 1 MiB — balances write-call overhead against peak memory per upload.
_UPLOAD_CHUNK = 1 * 1024 * 1024


class ChannelIn(BaseModel):
    name: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_\-\.]*$")
    description: str | None = None
    private: bool = False
    storage_prefix: str | None = None
    mirror_url: str | None = Field(
        default=None,
        max_length=512,
        pattern=r"^https?://",
        description="Upstream channel URL for proxy/cache mode, e.g. https://conda.anaconda.org/conda-forge",
    )
    mirror_cache_seconds: int = Field(default=900, ge=0, le=86400)


class ChannelOut(BaseModel):
    id: int
    name: str
    description: str | None
    private: bool
    storage_prefix: str
    mirror_url: str | None
    mirror_cache_seconds: int
    # Caller's effective permission on this channel: reader | writer | owner
    # | admin | None. Lets the frontend render action buttons without a
    # second round-trip. None means the caller can't see this row, which
    # shouldn't happen in practice (we filter the listing first).
    my_role: str | None = None

    model_config = {"from_attributes": True}


class MemberIn(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    role: str = Field(pattern=r"^(reader|writer|owner)$")


class MemberPatch(BaseModel):
    role: str = Field(pattern=r"^(reader|writer|owner)$")


class MemberOut(BaseModel):
    user_id: int
    email: str | None
    username: str | None
    role: str

    model_config = {"from_attributes": True}


def _channel_out(channel: Channel, my_role: str | None) -> ChannelOut:
    return ChannelOut(
        id=channel.id,
        name=channel.name,
        description=channel.description,
        private=channel.private,
        storage_prefix=channel.storage_prefix,
        mirror_url=channel.mirror_url,
        mirror_cache_seconds=channel.mirror_cache_seconds,
        my_role=my_role,
    )


async def _batch_my_roles(session, user: User | None, channel_ids: list[int]) -> dict[int, str]:
    """Resolve the caller's role on each of the given channels in one query.

    For server-admin we don't hit the table — their level is ``"admin"``
    for everything. For anon we leave the dict empty and rely on the
    per-channel ``private=False`` fallback in _level_for_row.
    """
    if user is None or not channel_ids:
        return {}
    if user.role == "admin":
        return {cid: "admin" for cid in channel_ids}
    result = await session.execute(
        select(ChannelMember.channel_id, ChannelMember.role).where(
            ChannelMember.user_id == user.id,
            ChannelMember.channel_id.in_(channel_ids),
        )
    )
    return {cid: role for cid, role in result.all()}


def _level_for_row(
    channel: Channel, user: User | None, membership: dict[int, str]
) -> AccessLevel | None:
    """Mirror channel_access_level's decision without an extra DB call,
    given a pre-fetched membership map (from _batch_my_roles)."""
    role = membership.get(channel.id)
    if role is not None:
        return role  # type: ignore[return-value]
    if not channel.private:
        return "reader"
    return None


@router.get("", response_model=list[ChannelOut])
async def list_channels(
    session: SessionDep,
    user: Annotated[User | None, Depends(current_user_optional)],
) -> list[ChannelOut]:
    result = await session.execute(visible_channels_stmt(user))
    channels = list(result.scalars())
    membership = await _batch_my_roles(session, user, [c.id for c in channels])
    return [_channel_out(c, _level_for_row(c, user, membership)) for c in channels]


@router.post("", response_model=ChannelOut, status_code=status.HTTP_201_CREATED)
async def create_channel(
    payload: ChannelIn,
    session: SessionDep,
    admin: Annotated[User, Depends(require_admin)],
) -> ChannelOut:
    existing = await session.execute(select(Channel).where(Channel.name == payload.name))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="channel exists")
    channel = Channel(
        name=payload.name,
        description=payload.description,
        private=payload.private,
        storage_prefix=payload.storage_prefix or payload.name,
        mirror_url=payload.mirror_url.rstrip("/") if payload.mirror_url else None,
        mirror_cache_seconds=payload.mirror_cache_seconds,
    )
    session.add(channel)
    await session.flush()
    # The creating admin is auto-enrolled as the channel's first owner so
    # non-server-admin users get a working management surface after
    # admin-driven channel creation. Server-admins bypass per-channel
    # rules anyway, but the row makes the ownership fact explicit.
    session.add(ChannelMember(channel_id=channel.id, user_id=admin.id, role="owner"))
    await audit.record(
        session,
        admin,
        "channel.create",
        channel_name=channel.name,
        meta={"private": channel.private, "mirror_url": channel.mirror_url},
    )
    await session.commit()
    await session.refresh(channel)
    return _channel_out(channel, my_role="admin")


@router.get("/{name}", response_model=ChannelOut)
async def get_channel(
    name: str,
    session: SessionDep,
    user: Annotated[User | None, Depends(current_user_optional)],
) -> ChannelOut:
    channel = await visible_channel_or_404(session, name, user)
    level = await channel_access_level(session, channel, user)
    return _channel_out(channel, my_role=level)


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_channel(
    name: str,
    session: SessionDep,
    background: BackgroundTasks,
    user: Annotated[User, Depends(current_user)],
    channel: Annotated[Channel, Depends(require_channel_owner)],
) -> None:
    _ = name  # consumed by the dep
    # Cascade via relationships — Package + PackageVersion + ChannelMember
    # rows all go. Blob cleanup runs in the background after the commit:
    # listing + deleting thousands of conda-forge mirror objects can take
    # minutes, and we don't want the HTTP response to hang on that. If
    # the background task fails halfway, the DB row is already gone —
    # orphan bytes in storage are a cleanable nuisance, not a correctness
    # bug.
    channel_name = channel.name
    storage_prefix = channel.storage_prefix
    await session.delete(channel)
    await audit.record(session, user, "channel.delete", channel_name=channel_name)
    await session.commit()
    background.add_task(_wipe_channel_storage, channel_name, storage_prefix)


async def _wipe_channel_storage(channel_name: str, storage_prefix: str) -> None:
    """Remove every object under a channel's storage prefix.

    Runs detached from the request so a large mirror cache (tens of
    thousands of .conda + shard files) doesn't hang the deletion
    response. Errors are logged, not raised — the DB row is already gone
    and there's no user waiting.
    """
    prefix = storage_prefix.strip("/") + "/"
    try:
        storage = get_storage()
        deleted = await storage.delete_prefix(prefix)
        log.info(
            "channel.storage_wiped",
            channel=channel_name,
            prefix=prefix,
            deleted=deleted,
        )
    except Exception as exc:
        log.exception(
            "channel.storage_wipe_failed",
            channel=channel_name,
            prefix=prefix,
            error=str(exc),
        )


@router.post("/{name}/reindex", status_code=status.HTTP_202_ACCEPTED)
async def trigger_reindex(
    name: str,
    background: BackgroundTasks,
    session: SessionDep,
    user: Annotated[User, Depends(current_user)],
    channel: Annotated[Channel, Depends(require_channel_writer)],
) -> dict[str, str]:
    _ = name
    await audit.record(session, user, "channel.reindex", channel_name=channel.name)
    await session.commit()
    background.add_task(_reindex_background, channel.name)
    return {"status": "accepted", "channel": channel.name}


# --- member management --------------------------------------------------


@router.get("/{name}/members", response_model=list[MemberOut])
async def list_members(
    name: str,
    session: SessionDep,
    channel: Annotated[Channel, Depends(require_channel_owner)],
) -> list[MemberOut]:
    _ = name
    result = await session.execute(
        select(ChannelMember, User)
        .join(User, User.id == ChannelMember.user_id)
        .where(ChannelMember.channel_id == channel.id)
        .order_by(User.email)
    )
    return [
        MemberOut(
            user_id=member.user_id,
            email=user.email,
            username=user.username,
            role=member.role,
        )
        for member, user in result.all()
    ]


@router.post(
    "/{name}/members",
    response_model=MemberOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_member(
    name: str,
    payload: MemberIn,
    session: SessionDep,
    actor: Annotated[User, Depends(current_user)],
    channel: Annotated[Channel, Depends(require_channel_owner)],
) -> MemberOut:
    _ = name
    # Users must exist in our DB before they can be granted access. Pending
    # invitations (invite-by-email for someone who's never logged in) are
    # intentionally out of scope for the first cut.
    user_result = await session.execute(select(User).where(User.email == payload.email))
    target = user_result.scalar_one_or_none()
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no user with that email — they need to log in at least once first",
        )
    existing = await session.execute(
        select(ChannelMember).where(
            ChannelMember.channel_id == channel.id,
            ChannelMember.user_id == target.id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="user is already a member — use PATCH to change their role",
        )
    member = ChannelMember(channel_id=channel.id, user_id=target.id, role=payload.role)
    session.add(member)
    await audit.record(
        session,
        actor,
        "member.add",
        channel_name=channel.name,
        target=target.email,
        meta={"role": payload.role, "user_id": target.id},
    )
    await session.commit()
    return MemberOut(
        user_id=target.id,
        email=target.email,
        username=target.username,
        role=payload.role,
    )


@router.patch("/{name}/members/{user_id}", response_model=MemberOut)
async def update_member(
    name: str,
    user_id: int,
    payload: MemberPatch,
    session: SessionDep,
    actor: Annotated[User, Depends(current_user)],
    channel: Annotated[Channel, Depends(require_channel_owner)],
) -> MemberOut:
    _ = name
    member = await _get_member_or_404(session, channel.id, user_id)
    # Guard against orphaning a channel: don't let the last owner demote
    # themselves (or be demoted). Server admins can still rescue a channel
    # via the CLI regardless of membership.
    previous_role = member.role
    if member.role == "owner" and payload.role != "owner":
        await _assert_not_last_owner(session, channel.id)
    member.role = payload.role
    target_user = await session.get(User, member.user_id)
    await audit.record(
        session,
        actor,
        "member.update",
        channel_name=channel.name,
        target=target_user.email if target_user else None,
        meta={
            "user_id": member.user_id,
            "from_role": previous_role,
            "to_role": payload.role,
        },
    )
    await session.commit()
    return MemberOut(
        user_id=member.user_id,
        email=target_user.email if target_user else None,
        username=target_user.username if target_user else None,
        role=member.role,
    )


@router.delete("/{name}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    name: str,
    user_id: int,
    session: SessionDep,
    actor: Annotated[User, Depends(current_user)],
    channel: Annotated[Channel, Depends(require_channel_owner)],
) -> None:
    _ = name
    member = await _get_member_or_404(session, channel.id, user_id)
    if member.role == "owner":
        await _assert_not_last_owner(session, channel.id)
    target_user = await session.get(User, member.user_id)
    await session.delete(member)
    await audit.record(
        session,
        actor,
        "member.remove",
        channel_name=channel.name,
        target=target_user.email if target_user else None,
        meta={"user_id": member.user_id, "role": member.role},
    )
    await session.commit()


async def _get_member_or_404(session, channel_id: int, user_id: int) -> ChannelMember:
    result = await session.execute(
        select(ChannelMember).where(
            ChannelMember.channel_id == channel_id,
            ChannelMember.user_id == user_id,
        )
    )
    member = result.scalar_one_or_none()
    if member is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member not found")
    return member


async def _assert_not_last_owner(session, channel_id: int) -> None:
    """Raise 409 if the caller is about to remove the last owner of a channel.

    Called on both demote and delete paths. A channel without any owners
    is still reachable by server-admins via the CLI or the admin UI, but
    it becomes unmanageable through the normal API surface — better to
    reject than to orphan silently.
    """
    result = await session.execute(
        select(ChannelMember).where(
            ChannelMember.channel_id == channel_id,
            ChannelMember.role == "owner",
        )
    )
    owners = list(result.scalars())
    if len(owners) <= 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="cannot remove the last owner — promote another member first",
        )


# --- package upload / delete -------------------------------------------


@router.post("/{name}/packages", status_code=status.HTTP_202_ACCEPTED)
async def upload_packages(
    name: str,
    background: BackgroundTasks,
    session: SessionDep,
    user: Annotated[User, Depends(current_user)],
    channel: Annotated[Channel, Depends(require_channel_writer)],
    files: Annotated[list[UploadFile], File()],
) -> dict[str, Any]:
    """Upload one or more .conda / .tar.bz2 archives into a channel.

    The server extracts each archive's ``info/index.json`` via rattler and
    uses the declared subdir to decide where to store it — the client
    doesn't pick. A single background reindex runs after any successful
    upload so repodata.json reflects the new bytes. Requires writer+
    access on the channel; mirror channels are rejected regardless
    (upstream is authoritative).

    Partial success is allowed: individual file errors are reported in
    the ``results`` array while successfully-stored files still land.
    The response is always 202 as long as the request was well-formed.
    """
    _ = name
    if channel.mirror_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cannot upload to a mirror channel",
        )
    if not files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="no files uploaded",
        )

    upload_cfg = get_settings().upload
    # Cheap guard: sum the declared Content-Length values (via the
    # SpooledTemporaryFile sizes populated by the multipart parser). If
    # the aggregate already exceeds the request cap, reject before we
    # touch disk. Individual-file limits are enforced during spooling.
    declared_total = 0
    for upload in files:
        if upload.size is not None:
            declared_total += upload.size
    if declared_total > upload_cfg.max_total_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"request exceeds {upload_cfg.max_total_bytes // (1024 * 1024)} MiB "
                f"aggregate upload limit"
            ),
        )

    storage = get_storage()
    results: list[dict[str, Any]] = []
    stored_any = False
    running_total = 0

    for upload in files:
        filename = (upload.filename or "").strip()
        entry: dict[str, Any] = {"filename": filename}
        try:
            remaining_total = upload_cfg.max_total_bytes - running_total
            written = await _store_one_package(
                upload,
                channel,
                storage,
                filename,
                entry,
                max_file_bytes=upload_cfg.max_file_bytes,
                max_total_bytes=remaining_total,
            )
            running_total += written
            stored_any = True
        except HTTPException:
            raise
        except Exception as exc:
            entry.setdefault("status", "error")
            entry["error"] = str(exc)
        results.append(entry)

    if stored_any:
        background.add_task(_reindex_background, channel.name)

    for r in results:
        if r.get("status") == "stored":
            await audit.record(
                session,
                user,
                "package.upload",
                channel_name=channel.name,
                target=r["filename"],
                meta={"subdir": r.get("subdir"), "size": r.get("size")},
            )
    await session.commit()

    log.info(
        "upload.batch",
        channel=channel.name,
        count=len(results),
        stored=sum(1 for r in results if r.get("status") == "stored"),
        user=user.email,
    )
    return {"channel": channel.name, "results": results}


async def _store_one_package(
    upload: UploadFile,
    channel: Channel,
    storage,
    filename: str,
    entry: dict[str, Any],
    *,
    max_file_bytes: int,
    max_total_bytes: int,
) -> int:
    """Spool upload → /tmp → read subdir via rattler → stream into storage.

    Returns the number of bytes stored. Side-effect: mutates ``entry`` with
    the outcome (status, subdir, size). Temp file is always cleaned up.

    Enforces two caps: ``max_file_bytes`` for this single file, and
    ``max_total_bytes`` as the remaining budget within the batch. Whichever
    fires first aborts the spool before the tmpfile grows further.
    """
    if "/" in filename or ".." in filename or not filename:
        raise ValueError("invalid filename")
    if not filename.endswith((".conda", ".tar.bz2")):
        raise ValueError("unsupported format (expected .conda or .tar.bz2)")
    if parse_conda_filename(filename) is None:
        raise ValueError("filename must be <name>-<version>-<build>.<conda|tar.bz2>")

    # rattler reads from a filesystem path, so spool the upload to /tmp
    # (emptyDir in prod). Keeps us off the memory path and lets us stream
    # the same file into object storage once the subdir is known.
    suffix = ".conda" if filename.endswith(".conda") else ".tar.bz2"
    fd, tmp_path = tempfile.mkstemp(suffix=suffix, dir="/tmp")
    try:
        spooled = 0
        with os.fdopen(fd, "wb") as tmp:
            while True:
                chunk = await upload.read(_UPLOAD_CHUNK)
                if not chunk:
                    break
                spooled += len(chunk)
                if spooled > max_file_bytes:
                    raise ValueError(f"file exceeds {max_file_bytes // (1024 * 1024)} MiB limit")
                if spooled > max_total_bytes:
                    raise ValueError("request exceeds aggregate upload limit")
                tmp.write(chunk)

        try:
            index = rattler.IndexJson.from_package_archive(tmp_path)
        except Exception as exc:
            raise ValueError(f"cannot parse archive: {exc}") from exc

        subdir = index.subdir
        if subdir not in VALID_SUBDIRS:
            raise ValueError(f"archive declares subdir={subdir!r}, not a known conda subdir")

        key = f"{channel.storage_prefix.strip('/')}/{subdir}/{filename}"

        async def _chunks():
            # Python file IO is sync but fast; using a plain loop keeps the
            # deps minimal (no aiofiles).
            with open(tmp_path, "rb") as f:
                while True:
                    c = f.read(_UPLOAD_CHUNK)
                    if not c:
                        break
                    yield c

        content_type = "application/x-conda" if filename.endswith(".conda") else "application/x-tar"
        written = await storage.put_stream(
            key,
            _chunks(),
            content_type=content_type,
            content_disposition=f'attachment; filename="{filename}"',
        )

        UPLOADS_TOTAL.labels(channel=channel.name, subdir=subdir).inc()
        UPLOAD_BYTES.labels(channel=channel.name).inc(written)

        entry.update(
            {
                "status": "stored",
                "subdir": subdir,
                "size": written,
                "name": _package_name_str(index.name),
                "version": str(index.version),
                "build": index.build,
            }
        )
        return written
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)


def _package_name_str(name: Any) -> str:
    """Extract the bare package-name string from a rattler PackageName.

    ``str(rattler.PackageName("foo"))`` returns ``'PackageName("foo")'``
    — its repr — not the underlying name. Use the explicit ``.source``
    accessor instead (it preserves the recipe's casing; ``.normalized``
    would lowercase, which is fine when comparing but loses the original
    spelling for display).
    """
    if hasattr(name, "source"):
        return name.source
    if hasattr(name, "normalized"):
        return name.normalized
    return str(name)


class ImportItem(BaseModel):
    subdir: str = Field(..., max_length=32)
    filename: str = Field(..., max_length=512)


class ImportRequest(BaseModel):
    upstream_url: str = Field(..., max_length=512, pattern=r"^https?://")
    # Cap is sized for full transitive closures (ada-py + scipy stack
    # easily clears 200 entries), not the user's initial pick.
    packages: list[ImportItem] = Field(..., min_length=1, max_length=2000)
    # Extra subdirs to load into the solver. Required when the operator
    # picks a noarch package whose deps are platform-specific (e.g.
    # noarch ada-py depending on h5py): the UI presents a toggle group
    # so the operator picks which platforms' transitive deps to import.
    # Empty list = "deps are resolvable from the picked packages' own
    # subdirs only" (true for purely platform-specific picks).
    target_platforms: list[str] = Field(default_factory=list, max_length=10)


class PreviewItem(BaseModel):
    name: str
    version: str
    build: str
    subdir: str
    filename: str
    size: int | None
    depends: list[str]


class PreviewResult(BaseModel):
    upstream_url: str
    direct_requested: list[PreviewItem]
    transitive_new: list[PreviewItem]
    transitive_satisfied_locally: list[PreviewItem]
    total_new_bytes: int


# Permissive virtual-package shims keyed by target subdir. The pod can't
# detect virtuals for the requesting client's machine, but every modern
# conda-forge build pins __glibc / __osx / __win at a floor we can
# satisfy with a high enough version. False positives are acceptable
# here — the operator decides whether to import, then conda/pixi
# re-solves on the install side with real virtuals.
_VIRTUAL_PACKAGE_FAKES: dict[str, list[tuple[str, str, str]]] = {
    "linux-64": [
        ("__archspec", "1", "x86_64"),
        ("__linux", "6.0", "0"),
        ("__unix", "0", "0"),
        ("__glibc", "2.99", "0"),
    ],
    "linux-aarch64": [
        ("__archspec", "1", "aarch64"),
        ("__linux", "6.0", "0"),
        ("__unix", "0", "0"),
        ("__glibc", "2.99", "0"),
    ],
    "linux-ppc64le": [
        ("__archspec", "1", "ppc64le"),
        ("__linux", "6.0", "0"),
        ("__unix", "0", "0"),
        ("__glibc", "2.99", "0"),
    ],
    "osx-64": [
        ("__archspec", "1", "x86_64"),
        ("__osx", "14.0", "0"),
        ("__unix", "0", "0"),
    ],
    "osx-arm64": [
        ("__archspec", "1", "arm64"),
        ("__osx", "14.0", "0"),
        ("__unix", "0", "0"),
    ],
    "win-64": [
        ("__archspec", "1", "x86_64"),
        ("__win", "0", "0"),
    ],
}


def _virtual_packages_for(subdirs: set[str]) -> list[rattler.GenericVirtualPackage]:
    """Pick a virtual-package set covering every subdir in the request."""
    out: list[rattler.GenericVirtualPackage] = []
    seen: set[tuple[str, str, str]] = set()
    for subdir in subdirs:
        for triple in _VIRTUAL_PACKAGE_FAKES.get(subdir, []):
            if triple in seen:
                continue
            seen.add(triple)
            name, version, build = triple
            out.append(
                rattler.GenericVirtualPackage(
                    rattler.PackageName(name),
                    rattler.Version(version),
                    build,
                )
            )
    return out


# Module-level Gateway shared across import-preview requests in this
# pod. rattler.Gateway holds parsed repodata in memory after the first
# fetch; building a new Gateway per request would re-parse a few hundred
# MB of repodata on every call (and OOM-killed the pod when multiple
# platforms were toggled). Keeping one process-wide instance lets the
# in-memory cache survive across requests for the lifetime of the pod.
_GATEWAY: rattler.Gateway | None = None
_GATEWAY_CACHE_DIR = pathlib.Path("/tmp/rattler-cache")


def _get_solver_gateway() -> rattler.Gateway:
    global _GATEWAY
    if _GATEWAY is None:
        _GATEWAY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _GATEWAY = rattler.Gateway(cache_dir=_GATEWAY_CACHE_DIR)
    return _GATEWAY


@router.post("/{name}/import/preview", response_model=PreviewResult)
async def import_preview(
    name: str,
    payload: ImportRequest,
    session: SessionDep,
    user: Annotated[User, Depends(current_user)],
    channel: Annotated[Channel, Depends(require_channel_writer)],
) -> PreviewResult:
    """Resolve the dependency closure for an import request without persisting.

    Builds an exact-pin MatchSpec for each requested ``(name, version,
    build)`` (parsed from the filename), then runs ``rattler.solve``
    against the upstream channel. The resulting ``RepoDataRecord`` set
    is the full transitive closure including dependencies the operator
    didn't pick. Each record is then categorised against the target
    channel's existing rows:

    - ``direct_requested``: the files the operator picked.
    - ``transitive_new``: deps that aren't in this channel yet — the
      ones an "Import all" would actually fetch.
    - ``transitive_satisfied_locally``: deps already present.

    Mirror channels are rejected for the same reason as ``import``.

    Network egress: the solve fetches the upstream's repodata into a
    rattler cache under ``/tmp``. First call per pod for a given
    upstream subdir takes a few seconds; subsequent calls are warm.
    """
    _ = name, user
    if channel.mirror_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cannot import into a mirror channel",
        )

    upstream_url = payload.upstream_url.rstrip("/")

    # Each item becomes (spec_str, pick_subdir). We need pick_subdir per
    # entry so noarch picks fan out across target_platforms while a
    # linux-64 pick stays scoped to linux.
    spec_jobs: list[tuple[str, str]] = []
    for item in payload.packages:
        if item.subdir not in VALID_SUBDIRS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"invalid subdir {item.subdir!r}",
            )
        parsed = parse_conda_filename(item.filename)
        if parsed is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unparseable filename {item.filename!r}",
            )
        spec_name, spec_version, spec_build = parsed
        # MatchSpec exact-triple form is space-separated. Using "=="
        # for the version pin leaves a leading "=" attached to the
        # build string, which rattler rejects as an invalid character.
        spec_jobs.append((f"{spec_name} =={spec_version} {spec_build}", item.subdir))

    for plat in payload.target_platforms:
        if plat not in VALID_SUBDIRS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"invalid target platform {plat!r}",
            )

    try:
        gateway = _get_solver_gateway()
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"can't create rattler cache dir: {exc}",
        ) from exc

    # Solve once per (spec, target_subdir). Calling rattler.solve with
    # multiple host platforms in one shot picks ONE platform's worth of
    # deps (constrained by virtual packages) — for noarch picks that
    # produced a non-deterministic mix of linux + win deps. We instead
    # iterate explicitly: for a noarch pick, run the solve once per
    # target platform so the union covers every requested platform's
    # full closure.
    records: list[Any] = []
    seen: set[tuple[str, str]] = set()
    for spec, pick_subdir in spec_jobs:
        if pick_subdir == "noarch":
            target_subdirs = list(payload.target_platforms) or ["linux-64"]
        else:
            target_subdirs = [pick_subdir]

        for target_subdir in target_subdirs:
            solve_subdirs = {target_subdir, "noarch"}
            virtuals = _virtual_packages_for({target_subdir})
            try:
                batch = await rattler.solve(
                    sources=[upstream_url],
                    specs=[spec],
                    gateway=gateway,
                    platforms=sorted(solve_subdirs),
                    virtual_packages=virtuals,
                    timeout=_dt.timedelta(seconds=60),
                )
            except Exception as exc:
                # rattler raises a SolverError with a structured "no
                # candidates" message — surface it verbatim, prefixed
                # with the failing spec + platform so the UI can
                # pinpoint which pick broke.
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(f"solve failed for {spec!r} on {target_subdir}: {exc}"),
                ) from exc
            for r in batch:
                key = (r.subdir, r.file_name)
                if key in seen:
                    continue
                seen.add(key)
                records.append(r)

    direct_set = {(p.subdir, p.filename) for p in payload.packages}
    existing_result = await session.execute(
        select(PackageVersion.subdir, PackageVersion.filename)
        .join(Package, Package.id == PackageVersion.package_id)
        .where(Package.channel_id == channel.id)
    )
    existing = {(s, f) for s, f in existing_result}

    direct: list[PreviewItem] = []
    transitive_new: list[PreviewItem] = []
    transitive_existing: list[PreviewItem] = []
    total_new_bytes = 0

    for r in records:
        item = PreviewItem(
            name=_package_name_str(r.name),
            version=str(r.version),
            build=r.build,
            subdir=r.subdir,
            filename=r.file_name,
            size=r.size,
            depends=list(r.depends or []),
        )
        key = (r.subdir, r.file_name)
        if key in direct_set:
            direct.append(item)
        elif key in existing:
            transitive_existing.append(item)
        else:
            transitive_new.append(item)
            if r.size:
                total_new_bytes += r.size

    return PreviewResult(
        upstream_url=upstream_url,
        direct_requested=direct,
        transitive_new=transitive_new,
        transitive_satisfied_locally=transitive_existing,
        total_new_bytes=total_new_bytes,
    )


# How many upstream packages we fetch concurrently per import job.
# Tuned for a modest single-node deployment: bounded enough that one big import
# doesn't saturate egress + connection pools, parallel enough that an
# 880 MB / ~50-file conda-forge dep closure finishes in tens of seconds
# instead of minutes. Bump if upstream + storage tier can take it.
_IMPORT_CONCURRENCY = 4


# Track in-flight import-runner tasks so they're not garbage-collected
# before the runner finishes. asyncio.create_task only holds a weak ref
# from the outside, and a task that nothing strong-refs can be cancelled
# unexpectedly. This set is purely about lifetime; the runner self-
# removes its entry on completion.
_RUNNING_IMPORTS: set[asyncio.Task[None]] = set()


class ImportJobOut(BaseModel):
    id: int
    channel: str
    upstream_url: str
    status: str
    total_count: int
    completed_count: int
    failed_count: int
    written_bytes: int
    current_filename: str | None
    error: str | None
    results: list[dict[str, Any]]
    created_at: datetime
    finished_at: datetime | None


def _job_to_out(job: ImportJob, channel_name: str) -> ImportJobOut:
    return ImportJobOut(
        id=job.id,
        channel=channel_name,
        upstream_url=job.upstream_url,
        status=job.status,
        total_count=job.total_count,
        completed_count=job.completed_count,
        failed_count=job.failed_count,
        written_bytes=job.written_bytes,
        current_filename=job.current_filename,
        error=job.error,
        results=list(job.results or []),
        created_at=job.created_at,
        finished_at=job.finished_at,
    )


@router.post("/{name}/import", status_code=status.HTTP_202_ACCEPTED)
async def import_packages(
    name: str,
    payload: ImportRequest,
    session: SessionDep,
    user: Annotated[User, Depends(current_user)],
    channel: Annotated[Channel, Depends(require_channel_writer)],
) -> dict[str, Any]:
    """Enqueue an import-from-upstream job and return its id.

    The HTTP request returns immediately with the new ``ImportJob`` row
    in ``pending`` state; the actual fetch + validate + store work runs
    in a background task that updates ``completed_count`` /
    ``written_bytes`` / ``results`` as it goes. Clients poll
    ``GET /channels/{name}/import/jobs/{id}`` until ``status`` is
    terminal (``completed`` or ``failed``).

    Mirror channels are rejected — an open mirror already proxies
    everything, importing on top wouldn't make sense.
    """
    _ = name
    if channel.mirror_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cannot import into a mirror channel",
        )

    upstream_url = payload.upstream_url.rstrip("/")

    job = ImportJob(
        channel_id=channel.id,
        user_id=user.id,
        upstream_url=upstream_url,
        status="pending",
        total_count=len(payload.packages),
        results=[],
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)

    # Pre-create Package rows for every distinct name in the payload.
    # The parallel runner inserts PackageVersion rows but never touches
    # the Package table — keeps us off the (channel_id, name) unique
    # constraint when two parallel workers would otherwise race.
    distinct_names: set[str] = set()
    for item in payload.packages:
        parsed = parse_conda_filename(item.filename)
        if parsed is not None:
            distinct_names.add(parsed[0])
    for pkg_name in distinct_names:
        await _get_or_create_package_row(session, channel.id, pkg_name)
    await session.commit()

    items = [(it.subdir, it.filename) for it in payload.packages]
    task = asyncio.create_task(
        _run_import_job(
            job_id=job.id,
            channel_id=channel.id,
            channel_name=channel.name,
            user_email=user.email,
            upstream_url=upstream_url,
            items=items,
        )
    )
    _RUNNING_IMPORTS.add(task)
    task.add_done_callback(_RUNNING_IMPORTS.discard)

    log.info(
        "import.job_enqueued",
        job_id=job.id,
        channel=channel.name,
        upstream=upstream_url,
        count=len(items),
        user=user.email,
    )
    return {
        "job_id": job.id,
        "channel": channel.name,
        "upstream_url": upstream_url,
        "status_url": f"/api/channels/{channel.name}/import/jobs/{job.id}",
    }


@router.get(
    "/{name}/import/jobs/{job_id}",
    response_model=ImportJobOut,
)
async def get_import_job(
    name: str,
    job_id: int,
    session: SessionDep,
    channel: Annotated[Channel, Depends(require_channel_writer)],
) -> ImportJobOut:
    """Progress + final results for an import job. Polled by the UI."""
    _ = name
    result = await session.execute(
        select(ImportJob).where(
            ImportJob.id == job_id,
            ImportJob.channel_id == channel.id,
        )
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="import job not found",
        )
    return _job_to_out(job, channel.name)


async def _run_import_job(
    *,
    job_id: int,
    channel_id: int,
    channel_name: str,
    user_email: str | None,
    upstream_url: str,
    items: list[tuple[str, str]],
) -> None:
    """Background runner for a single import job.

    Owns its own sessions (the request that created the job is long
    gone). Downloads up to ``_IMPORT_CONCURRENCY`` files in parallel,
    each in its own AsyncSession (SQLAlchemy AsyncSession isn't safe
    to share across coroutines). Updates the job row periodically.
    """
    sm = get_sessionmaker()
    upload_cfg = get_settings().upload
    storage = get_storage()
    client = _shared_http_client()

    # Mark the job running. A separate session — committed quickly so
    # the GET status endpoint can pick it up.
    async with sm() as s:
        job = await s.get(ImportJob, job_id)
        if job is None:
            log.warning("import.job_missing", job_id=job_id)
            return
        job.status = "running"
        await s.commit()

    sem = asyncio.Semaphore(_IMPORT_CONCURRENCY)
    progress_lock = asyncio.Lock()  # serialise writes to the job row.

    async def _record_progress(
        *,
        entry: dict[str, Any],
        ok: bool,
        written: int,
    ) -> None:
        async with progress_lock, sm() as s:
            j = await s.get(ImportJob, job_id)
            if j is None:
                return
            j.results = [*list(j.results or []), entry]
            if ok:
                j.completed_count += 1
                j.written_bytes += written
            else:
                j.failed_count += 1
            # Most recent filename touched — useful as a "now fetching X" hint.
            j.current_filename = entry.get("filename")
            await s.commit()

    async def _process(item: tuple[str, str]) -> None:
        subdir, filename = item
        entry: dict[str, Any] = {"filename": filename, "subdir": subdir}
        async with sem:
            try:
                async with sm() as s:
                    chan = await s.get(Channel, channel_id)
                    if chan is None:
                        raise RuntimeError("channel disappeared")
                    written = await _import_one_package(
                        client=client,
                        upstream_url=upstream_url,
                        channel=chan,
                        storage=storage,
                        session=s,
                        subdir=subdir,
                        filename=filename,
                        entry=entry,
                        max_file_bytes=upload_cfg.max_file_bytes,
                        # Per-file cap only — aggregate is enforced via job
                        # quota at preview time. Pass the per-file cap as
                        # the per-call max_total too, since each call has
                        # its own session and we don't track running totals
                        # across the parallel workers.
                        max_total_bytes=upload_cfg.max_file_bytes,
                    )
                    if user_email:
                        # Re-fetch the actor so audit.record has a User row
                        # bound to this session.
                        actor_q = await s.execute(select(User).where(User.email == user_email))
                        actor = actor_q.scalar_one_or_none()
                        if actor is not None:
                            await audit.record(
                                s,
                                actor,
                                "package.import",
                                channel_name=channel_name,
                                target=filename,
                                meta={
                                    "subdir": subdir,
                                    "size": written,
                                    "upstream": upstream_url,
                                    "job_id": job_id,
                                },
                            )
                    await s.commit()
                await _record_progress(entry=entry, ok=True, written=written)
            except Exception as exc:
                entry.setdefault("status", "error")
                entry["error"] = str(exc)
                await _record_progress(entry=entry, ok=False, written=0)

    try:
        await asyncio.gather(*(_process(it) for it in items))
        # Reindex once at the end — the workers added rows in parallel
        # and we only want one repodata regen per job.
        await _reindex_background(channel_name)
        async with sm() as s:
            j = await s.get(ImportJob, job_id)
            if j is not None:
                # Partial success is still "completed"; callers inspect
                # failed_count and per-item results to see what broke.
                j.status = "completed"
                j.finished_at = _dt.datetime.now(_dt.UTC)
                await s.commit()
        log.info(
            "import.job_completed",
            job_id=job_id,
            channel=channel_name,
            completed=len(items),
        )
    except Exception as exc:
        log.exception(
            "import.job_failed",
            job_id=job_id,
            channel=channel_name,
        )
        async with sm() as s:
            j = await s.get(ImportJob, job_id)
            if j is not None:
                j.status = "failed"
                j.error = str(exc)
                j.finished_at = _dt.datetime.now(_dt.UTC)
                await s.commit()


async def _import_one_package(
    *,
    client: httpx.AsyncClient,
    upstream_url: str,
    channel: Channel,
    storage,
    session,
    subdir: str,
    filename: str,
    entry: dict[str, Any],
    max_file_bytes: int,
    max_total_bytes: int,
) -> int:
    """Pull one .conda from upstream and persist it. Returns bytes written.

    Side-effects: writes the object to storage, inserts/updates the
    Package + PackageVersion rows in the open session. The session is
    committed by the caller after iterating the batch.
    """
    if subdir not in VALID_SUBDIRS:
        raise ValueError(f"invalid subdir {subdir!r}")
    if "/" in filename or ".." in filename or not filename:
        raise ValueError("invalid filename")
    if not filename.endswith((".conda", ".tar.bz2")):
        raise ValueError("unsupported format (expected .conda or .tar.bz2)")
    if parse_conda_filename(filename) is None:
        raise ValueError("filename must be <name>-<version>-<build>.<conda|tar.bz2>")

    key = f"{channel.storage_prefix.strip('/')}/{subdir}/{filename}"
    existing_meta = await storage.head(key)
    if existing_meta is not None:
        raise ValueError("already present in this channel")

    upstream_object_url = f"{upstream_url}/{subdir}/{filename}"

    suffix = ".conda" if filename.endswith(".conda") else ".tar.bz2"
    fd, tmp_path = tempfile.mkstemp(suffix=suffix, dir="/tmp")
    try:
        spooled = 0
        try:
            async with client.stream("GET", upstream_object_url) as resp:
                if resp.status_code == 404:
                    raise ValueError("upstream returned 404")
                if resp.status_code >= 400:
                    raise ValueError(f"upstream returned {resp.status_code}")
                with os.fdopen(fd, "wb") as tmp:
                    async for chunk in resp.aiter_bytes(_UPLOAD_CHUNK):
                        spooled += len(chunk)
                        if spooled > max_file_bytes:
                            raise ValueError(
                                f"file exceeds {max_file_bytes // (1024 * 1024)} MiB limit"
                            )
                        if spooled > max_total_bytes:
                            raise ValueError("request exceeds aggregate upload limit")
                        tmp.write(chunk)
        except httpx.HTTPError as exc:
            raise ValueError(f"upstream fetch failed: {exc}") from exc

        try:
            index = rattler.IndexJson.from_package_archive(tmp_path)
        except Exception as exc:
            raise ValueError(f"cannot parse archive: {exc}") from exc

        # Validate the archive's metadata matches what the operator
        # asked for — protects against typo'd or malicious filenames.
        archive_subdir = index.subdir
        if archive_subdir != subdir:
            raise ValueError(f"archive declares subdir={archive_subdir!r}, expected {subdir!r}")
        parsed = parse_conda_filename(filename)
        assert parsed is not None  # validated above
        fn_name, fn_version, fn_build = parsed
        archive_name = _package_name_str(index.name)
        if archive_name.lower() != fn_name.lower():
            raise ValueError(f"archive name={archive_name!r} disagrees with filename {fn_name!r}")
        if str(index.version) != fn_version:
            raise ValueError(
                f"archive version={str(index.version)!r} disagrees with filename {fn_version!r}"
            )
        if index.build != fn_build:
            raise ValueError(f"archive build={index.build!r} disagrees with filename {fn_build!r}")

        size = os.path.getsize(tmp_path)

        async def _chunks():
            with open(tmp_path, "rb") as f:
                while True:
                    c = f.read(_UPLOAD_CHUNK)
                    if not c:
                        break
                    yield c

        content_type = "application/x-conda" if filename.endswith(".conda") else "application/x-tar"
        written = await storage.put_stream(
            key,
            _chunks(),
            content_type=content_type,
            content_disposition=f'attachment; filename="{filename}"',
        )

        # Create rows inline so imported_from lands. The background
        # reindex will find these rows existing and update other fields
        # (sha/size from rattler-built repodata) without touching
        # imported_from — it's not in `_apply_version`'s field list.
        pkg = await _get_or_create_package_row(session, channel.id, fn_name)
        await session.flush()
        ts = int(index.timestamp.timestamp() * 1000) if getattr(index, "timestamp", None) else None
        version_row = PackageVersion(
            package_id=pkg.id,
            version=fn_version,
            build=fn_build,
            build_number=int(getattr(index, "build_number", 0) or 0),
            subdir=subdir,
            filename=filename,
            size=size,
            depends=list(getattr(index, "depends", []) or []),
            constrains=list(getattr(index, "constrains", []) or []),
            package_timestamp=(
                datetime.fromtimestamp(ts / 1000, tz=_dt.UTC) if ts is not None else None
            ),
            info={"name": fn_name, "version": fn_version, "build": fn_build},
            imported_from=upstream_object_url,
        )
        # The archive is already spooled to /tmp for the index.json read,
        # so pulling info/about.json out of it costs one more seek into
        # the same file — no second fetch, no storage round-trip. Stamping
        # about_fetched_at here is also what stops the background reindex
        # from re-downloading the artifact: this row has no sha256 yet, so
        # the reindex will see it as "changed", and the stamp is how
        # _should_recapture_about tells that apart from replaced bytes.
        apply_about(version_row, read_package_about(tmp_path))
        session.add(version_row)

        UPLOADS_TOTAL.labels(channel=channel.name, subdir=subdir).inc()
        UPLOAD_BYTES.labels(channel=channel.name).inc(written)

        entry.update(
            {
                "status": "stored",
                "size": written,
                "name": fn_name,
                "version": fn_version,
                "build": fn_build,
                "imported_from": upstream_object_url,
            }
        )
        return written
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)


async def _get_or_create_package_row(session, channel_id: int, name: str) -> Package:
    """Mirror of indexer._get_or_create_package, scoped here to avoid an
    import cycle. Idempotent."""
    result = await session.execute(
        select(Package).where(Package.channel_id == channel_id, Package.name == name)
    )
    pkg = result.scalar_one_or_none()
    if pkg is None:
        pkg = Package(channel_id=channel_id, name=name)
        session.add(pkg)
        await session.flush()
    return pkg


@router.delete(
    "/{name}/packages/{subdir}/{filename}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_package(
    name: str,
    subdir: str,
    filename: str,
    session: SessionDep,
    background: BackgroundTasks,
    user: Annotated[User, Depends(current_user)],
    channel: Annotated[Channel, Depends(require_channel_writer)],
) -> None:
    """Remove a single uploaded .conda / .tar.bz2 from a non-mirror channel.

    Deletes the bytes from object storage and the corresponding rows; if
    it was the last version of a package, the package row goes too. A
    background reindex regenerates repodata so clients stop seeing it.
    Requires writer+; mirror channels are rejected because upstream is
    authoritative.
    """
    _ = name
    if channel.mirror_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cannot delete from a mirror channel",
        )
    if subdir not in VALID_SUBDIRS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid subdir")
    if "/" in filename or ".." in filename or not filename.endswith((".conda", ".tar.bz2")):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid filename")

    result = await session.execute(
        select(PackageVersion)
        .join(Package, Package.id == PackageVersion.package_id)
        .where(
            Package.channel_id == channel.id,
            PackageVersion.subdir == subdir,
            PackageVersion.filename == filename,
        )
    )
    version = result.scalar_one_or_none()
    if version is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="package version not found"
        )

    storage = get_storage()
    key = f"{channel.storage_prefix.strip('/')}/{subdir}/{filename}"
    # Object already gone — keep going so the DB reaches a consistent state.
    with contextlib.suppress(FileNotFoundError):
        await storage.delete(key)

    package_id = version.package_id
    await session.delete(version)
    await session.flush()

    remaining = await session.execute(
        select(PackageVersion).where(PackageVersion.package_id == package_id).limit(1)
    )
    if remaining.scalar_one_or_none() is None:
        pkg = await session.get(Package, package_id)
        if pkg is not None:
            await session.delete(pkg)

    await audit.record(
        session,
        user,
        "package.delete",
        channel_name=channel.name,
        target=filename,
        meta={"subdir": subdir},
    )
    await session.commit()

    PACKAGE_DELETES.labels(channel=channel.name, subdir=subdir).inc()

    log.info(
        "upload.deleted",
        channel=channel.name,
        subdir=subdir,
        filename=filename,
        user=user.email,
    )
    background.add_task(_reindex_background, channel.name)


# --- about-metadata backfill -------------------------------------------

#: Rows one admin-triggered backfill run will open. The pass is
#: resumable, so this is a bound on a single click rather than on the
#: total work: when a run stops here the response says so and the
#: operator can start another. It exists because each row means pulling
#: a package archive out of object storage, and an unbounded click on a
#: large channel is an unpleasant surprise on metered egress.
_BACKFILL_JOB_LIMIT = 1000

#: Strong refs to in-flight backfill runners, for the same reason as
#: ``_RUNNING_IMPORTS``: asyncio.create_task only holds a weak ref, and
#: a task nothing strong-refs can be collected mid-run.
_RUNNING_BACKFILLS: set[asyncio.Task[None]] = set()

_BACKFILL_KIND = "about_backfill"


class MaintenanceJobOut(BaseModel):
    id: int
    kind: str
    channel: str | None
    status: str
    total_count: int
    completed_count: int
    failed_count: int
    with_metadata_count: int
    current_target: str | None
    error: str | None
    created_at: datetime
    finished_at: datetime | None


def _maintenance_job_out(job: MaintenanceJob, channel_name: str | None) -> MaintenanceJobOut:
    return MaintenanceJobOut(
        id=job.id,
        kind=job.kind,
        channel=channel_name,
        status=job.status,
        total_count=job.total_count,
        completed_count=job.completed_count,
        failed_count=job.failed_count,
        with_metadata_count=job.with_metadata_count,
        current_target=job.current_target,
        error=job.error,
        created_at=job.created_at,
        finished_at=job.finished_at,
    )


@router.post("/{name}/backfill-about", status_code=status.HTTP_202_ACCEPTED)
async def trigger_about_backfill(
    name: str,
    session: SessionDep,
    user: Annotated[User, Depends(current_user)],
    channel: Annotated[Channel, Depends(require_channel_owner)],
) -> dict[str, Any]:
    """Start a pass that reads package metadata from stored archives.

    Versions indexed before the server captured ``info/about.json`` have
    no docs link, homepage or summary, and a reindex will not give them
    one — it only opens archives for versions that were added or
    changed. This is the deliberate pass over the rest.

    Returns immediately with a job id; the work runs in the background
    and the job row carries progress. Safe to run repeatedly: versions
    already inspected are skipped, so a second run only picks up what
    the first did not reach.
    """
    _ = name
    if channel.mirror_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="mirror channels have no stored versions to backfill",
        )

    # One at a time per channel. Two concurrent passes would select
    # overlapping rows and download the same archives twice — correct,
    # thanks to the stamp, but paid for twice.
    running = await session.scalar(
        select(func.count(MaintenanceJob.id)).where(
            MaintenanceJob.channel_id == channel.id,
            MaintenanceJob.kind == _BACKFILL_KIND,
            MaintenanceJob.status.in_(["pending", "running"]),
        )
    )
    if running:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a metadata backfill is already running for this channel",
        )

    pending = await count_pending(session, channel)
    if not pending:
        return {
            "status": "up-to-date",
            "channel": channel.name,
            "pending": 0,
            "job_id": None,
        }

    job = MaintenanceJob(
        kind=_BACKFILL_KIND,
        channel_id=channel.id,
        user_id=user.id,
        status="pending",
        total_count=min(pending, _BACKFILL_JOB_LIMIT),
    )
    session.add(job)
    await audit.record(
        session,
        user,
        "channel.backfill_about",
        channel_name=channel.name,
        meta={"pending": pending, "limit": _BACKFILL_JOB_LIMIT},
    )
    await session.commit()
    await session.refresh(job)

    task = asyncio.create_task(
        _run_about_backfill(job_id=job.id, channel_id=channel.id, channel_name=channel.name)
    )
    _RUNNING_BACKFILLS.add(task)
    task.add_done_callback(_RUNNING_BACKFILLS.discard)

    log.info(
        "about.backfill_enqueued",
        job_id=job.id,
        channel=channel.name,
        pending=pending,
        user=user.email,
    )
    return {
        "status": "accepted",
        "job_id": job.id,
        "channel": channel.name,
        "pending": pending,
        "status_url": f"/api/channels/{channel.name}/backfill-about/jobs/{job.id}",
    }


@router.get(
    "/{name}/backfill-about/jobs/{job_id}",
    response_model=MaintenanceJobOut,
)
async def get_about_backfill_job(
    name: str,
    job_id: int,
    session: SessionDep,
    channel: Annotated[Channel, Depends(require_channel_owner)],
) -> MaintenanceJobOut:
    """Progress + outcome for a backfill job. Polled by the UI."""
    _ = name
    result = await session.execute(
        select(MaintenanceJob).where(
            MaintenanceJob.id == job_id,
            MaintenanceJob.channel_id == channel.id,
            MaintenanceJob.kind == _BACKFILL_KIND,
        )
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="backfill job not found",
        )
    return _maintenance_job_out(job, channel.name)


async def _run_about_backfill(*, job_id: int, channel_id: int, channel_name: str) -> None:
    """Background runner for one admin-triggered backfill.

    Owns its own sessions — the request that created the job is long
    gone. The batch runner commits the version rows as it goes, and this
    mirrors those commits onto the job row so the UI's poll shows real
    progress rather than jumping from 0 to done.
    """
    sm = get_sessionmaker()
    storage = get_storage()

    async with sm() as session:
        job = await session.get(MaintenanceJob, job_id)
        if job is None:
            log.warning("about.backfill_job_missing", job_id=job_id)
            return
        job.status = "running"
        await session.commit()

    try:
        async with sm() as session:
            channel = await session.get(Channel, channel_id)
            if channel is None:
                raise RuntimeError("channel disappeared")

            async def _on_progress(stats: BackfillStats) -> None:
                # Separate session: the batch runner owns `session` and
                # is mid-transaction on the version rows.
                async with sm() as progress_session:
                    j = await progress_session.get(MaintenanceJob, job_id)
                    if j is None:
                        return
                    j.completed_count = stats.inspected
                    j.failed_count = stats.failed
                    j.with_metadata_count = stats.with_metadata
                    await progress_session.commit()

            stats = await backfill_about_batch(
                session,
                storage,
                channel,
                limit=_BACKFILL_JOB_LIMIT,
                concurrency=DEFAULT_CONCURRENCY,
                on_progress=_on_progress,
            )

        async with sm() as session:
            j = await session.get(MaintenanceJob, job_id)
            if j is not None:
                j.status = "completed"
                j.completed_count = stats.inspected
                j.failed_count = stats.failed
                j.with_metadata_count = stats.with_metadata
                j.total_count = stats.touched
                # Not an error — the run hit its own bound. Surfacing it
                # here is how the operator learns to click again.
                if stats.hit_limit:
                    j.error = "stopped at this run's limit; run again to continue"
                j.finished_at = _dt.datetime.now(_dt.UTC)
                await session.commit()

        log.info(
            "about.backfill_completed",
            job_id=job_id,
            channel=channel_name,
            inspected=stats.inspected,
            with_metadata=stats.with_metadata,
            failed=stats.failed,
        )
    except Exception as exc:
        log.exception("about.backfill_failed", job_id=job_id, channel=channel_name)
        async with sm() as session:
            j = await session.get(MaintenanceJob, job_id)
            if j is not None:
                j.status = "failed"
                j.error = str(exc)
                j.finished_at = _dt.datetime.now(_dt.UTC)
                await session.commit()


async def _reindex_background(channel_name: str) -> None:
    """Run a reindex in its own session so the request's session isn't held open."""
    sm = get_sessionmaker()
    with REINDEX_DURATION.labels(channel=channel_name).time():
        try:
            async with sm() as session:
                result = await session.execute(select(Channel).where(Channel.name == channel_name))
                channel = result.scalar_one_or_none()
                if channel is None:
                    log.warning("reindex.channel_missing", channel=channel_name)
                    REINDEX_RUNS.labels(channel=channel_name, result="missing").inc()
                    return
                outcome = await reindex_channel(session, get_storage(), channel)
                await session.commit()
                REINDEX_RUNS.labels(channel=channel_name, result="success").inc()
                log.info(
                    "reindex.finished",
                    channel=outcome.channel,
                    added=outcome.added,
                    updated=outcome.updated,
                    removed=outcome.removed,
                )
        except Exception as exc:
            REINDEX_RUNS.labels(channel=channel_name, result="failure").inc()
            log.exception("reindex.failed", channel=channel_name, error=str(exc))
