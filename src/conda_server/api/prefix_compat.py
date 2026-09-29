"""Upload endpoint speaking the prefix.dev protocol, for ``rattler-build publish``.

rattler-build (and ``rattler-build upload prefix``) picks its upload backend
from the ``--to`` URL. ``prefix://<host>/<channel>`` selects the prefix.dev
backend for any host, which sends:

    POST https://<host>/api/v1/upload/<channel>[?force=true]
    Authorization: Bearer <token>
    multipart/form-data: part "file" (the archive, with X-File-Name and
    X-File-SHA256 part headers), optionally part "attestation"

and reads the status code only: 2xx is success, 409 means "already there"
(which ``--skip-existing`` turns into a warning), 401/403/413/422 are
reported as-is, 5xx is retried.

This is a thin shim over the native upload path, so storage, indexing,
limits and audit behave identically. The one deliberate difference is
overwrite semantics: the native endpoint replaces an existing archive,
while this one follows prefix.dev and refuses unless ``force=true``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from conda_server.api.channels import (
    PackageExists,
    _publish_uploads,
    _store_one_package,
    record_uploads,
)
from conda_server.auth import current_user, require_channel_writer
from conda_server.config import get_settings
from conda_server.db import SessionDep
from conda_server.logging import get_logger
from conda_server.models import Channel, User
from conda_server.storage import get_storage

router = APIRouter(prefix="/api/v1", tags=["compat"])
log = get_logger(__name__)


@router.post("/upload/{name}", status_code=status.HTTP_201_CREATED)
async def prefix_upload(
    name: str,
    session: SessionDep,
    user: Annotated[User, Depends(current_user)],
    channel: Annotated[Channel, Depends(require_channel_writer)],
    file: Annotated[UploadFile, File()],
    attestation: Annotated[str | None, Form()] = None,
    force: bool = False,
) -> dict[str, Any]:
    """Upload one archive the way rattler-build's prefix backend sends it.

    The attestation part is accepted so clients that send one don't fail,
    but it is not stored — there is nowhere to serve it from yet.
    """
    _ = name, attestation
    if channel.mirror_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cannot upload to a mirror channel",
        )

    # The client names the file both in the part's filename and in a part
    # header; prefer the header since that is the one it hashes against.
    filename = (file.headers.get("x-file-name") or file.filename or "").strip()
    upload_cfg = get_settings().upload
    storage = get_storage()
    entry: dict[str, Any] = {"filename": filename}
    try:
        placed = await _store_one_package(
            file,
            channel,
            storage,
            filename,
            entry,
            max_file_bytes=upload_cfg.max_file_bytes,
            max_total_bytes=upload_cfg.max_total_bytes,
            replace=force,
            expected_sha256=file.headers.get("x-file-sha256"),
        )
    except PackageExists as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    await _publish_uploads(session, storage, channel, [placed])
    await record_uploads(session, user, channel, [entry])
    await session.commit()

    log.info(
        "upload.prefix_compat",
        channel=channel.name,
        filename=filename,
        replaced=entry.get("replaced"),
        user=user.email,
    )
    return {"channel": channel.name, **entry}
