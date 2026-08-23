"""Object storage abstraction backed by obstore.

Supports local filesystem, S3 (and S3-compatible like MinIO), Azure Blob, and GCS
through a single :class:`ObstoreStorage` wrapper. The :class:`Storage` ABC is kept
so tests can substitute in-memory or fake implementations.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import obstore
from obstore.store import AzureStore, GCSStore, LocalStore, ObjectStore, S3Store

from conda_server.config import StorageSettings, resolve_path


@dataclass(frozen=True)
class ObjectMeta:
    key: str
    size: int
    last_modified: float | None = None
    etag: str | None = None


class Storage(ABC):
    """Abstract object storage."""

    @abstractmethod
    async def put(self, key: str, data: bytes, content_type: str | None = None) -> None: ...

    @abstractmethod
    async def put_stream(
        self,
        key: str,
        chunks: AsyncIterator[bytes],
        *,
        content_type: str | None = None,
        content_disposition: str | None = None,
    ) -> int:
        """Stream chunks straight into storage; returns total bytes written.

        Prefer this over ``put`` when the upload is large (client uploads,
        mirror fetches) so the full payload never needs to fit in memory.

        ``content_type`` and ``content_disposition``, when set, are stored
        on the object so subsequent GETs (including presigned redirects)
        return them as response headers. Essential for .conda / .tar.bz2
        downloads — without Content-Disposition, browsers sniff the
        bytes and rename .conda files to .zip based on their ZIP magic.
        """

    @abstractmethod
    async def get(self, key: str) -> bytes: ...

    @abstractmethod
    def stream(self, key: str) -> AsyncIterator[bytes]:
        """Yield the object's bytes in chunks. For large artifacts, prefer this
        over ``get()`` so the full payload never needs to fit in memory at
        once."""

    @abstractmethod
    async def get_range(self, key: str, *, start: int, length: int) -> bytes:
        """Return ``length`` bytes of the object beginning at ``start``.

        This is the difference between reading a few kilobytes out of a
        package and moving the package: a ``.conda`` is a zip, and the
        central directory at its tail says exactly where the small
        metadata member sits, so two ranged reads replace a whole-object
        download. See ``conda_server.package_about.read_conda_about_ranged``.

        Semantics follow an HTTP range request: a range running past the
        end of the object yields the remainder rather than failing, and a
        non-positive ``length`` yields ``b""``. ``start`` is expected to
        be inside the object — callers learn its size from ``head`` — and
        a backend may reject a start at or past the end. Missing objects
        raise the same way ``get`` does.
        """

    @abstractmethod
    async def delete(self, key: str) -> None: ...

    async def delete_prefix(self, prefix: str) -> int:
        """Remove every object under ``prefix``. Returns the count deleted.

        Default implementation enumerates via ``list`` and calls ``delete``
        on each key. Backends with a native bulk-delete API (S3's
        DeleteObjects, etc.) can override for ~50x throughput on large
        channels.

        Per-key failures (NoSuchKey from a stale directory marker, a
        racing concurrent delete, transient 503) are logged and skipped
        rather than aborting the whole wipe — the caller's intent is
        "leave nothing under this prefix" and one phantom entry shouldn't
        keep gigabytes of real data orphaned.
        """
        import logging

        log = logging.getLogger(__name__)
        deleted = 0
        failed = 0
        async for meta in self.list(prefix):
            try:
                await self.delete(meta.key)
                deleted += 1
            except FileNotFoundError:
                # Already gone — count as success.
                deleted += 1
            except Exception:
                failed += 1
                log.warning(
                    "delete_prefix: skipped key=%r under prefix=%r",
                    meta.key,
                    prefix,
                    exc_info=True,
                )
        if failed:
            log.warning(
                "delete_prefix: %d/%d objects failed under prefix=%r",
                failed,
                deleted + failed,
                prefix,
            )
        return deleted

    @abstractmethod
    def list(self, prefix: str = "") -> AsyncIterator[ObjectMeta]: ...

    @abstractmethod
    async def head(self, key: str) -> ObjectMeta | None:
        """Return metadata for a single object, or None if it doesn't exist."""

    @abstractmethod
    async def presign_get(self, key: str, expires_in: int) -> str: ...


class ObstoreStorage(Storage):
    """Unified wrapper over any obstore ObjectStore (S3, Azure, GCS, Local, Memory)."""

    def __init__(self, store: ObjectStore, *, supports_signing: bool) -> None:
        self._store = store
        self._supports_signing = supports_signing

    async def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        # obstore handles content-type via extension; explicit override not wired yet.
        await obstore.put_async(self._store, key, data)

    async def put_stream(
        self,
        key: str,
        chunks: AsyncIterator[bytes],
        *,
        content_type: str | None = None,
        content_disposition: str | None = None,
    ) -> int:
        # LocalStore doesn't implement put_opts with attributes (the error
        # doesn't surface until .close()); skip them there. The repodata
        # router sets the right headers on StreamingResponse for local
        # backends anyway, so the download experience matches S3.
        pass_attributes = not isinstance(self._store, LocalStore)
        kwargs: dict[str, Any] = {}
        if pass_attributes and (content_type or content_disposition):
            attributes: dict[str, str] = {}
            if content_type:
                attributes["Content-Type"] = content_type
            if content_disposition:
                attributes["Content-Disposition"] = content_disposition
            kwargs["attributes"] = attributes
        writer = obstore.open_writer_async(self._store, key, **kwargs)
        total = 0
        try:
            async for chunk in chunks:
                await writer.write(chunk)
                total += len(chunk)
        finally:
            await writer.close()
        return total

    async def get(self, key: str) -> bytes:
        result = await obstore.get_async(self._store, key)
        buf = await result.bytes_async()
        return bytes(buf)

    async def stream(self, key: str) -> AsyncIterator[bytes]:
        result = await obstore.get_async(self._store, key)
        async for chunk in result.stream():
            yield bytes(chunk)

    async def get_range(self, key: str, *, start: int, length: int) -> bytes:
        # obstore rejects a zero-length range outright, so short-circuit
        # rather than making the callers guard every arithmetic result.
        if length <= 0:
            return b""
        buf = await obstore.get_range_async(self._store, key, start=start, length=length)
        return bytes(buf)

    async def delete(self, key: str) -> None:
        await obstore.delete_async(self._store, key)

    async def list(self, prefix: str = "") -> AsyncIterator[ObjectMeta]:
        stream = obstore.list(self._store, prefix=prefix or None)
        batches = await stream.collect_async()
        for entry in batches:
            ts = entry.get("last_modified")
            yield ObjectMeta(
                key=entry["path"],
                size=entry["size"],
                last_modified=ts.timestamp() if ts is not None else None,
                etag=entry.get("e_tag"),
            )

    async def head(self, key: str) -> ObjectMeta | None:
        try:
            entry = await obstore.head_async(self._store, key)
        except FileNotFoundError:
            return None
        ts = entry.get("last_modified")
        return ObjectMeta(
            key=entry["path"],
            size=entry["size"],
            last_modified=ts.timestamp() if ts is not None else None,
            etag=entry.get("e_tag"),
        )

    async def presign_get(self, key: str, expires_in: int) -> str:
        if not self._supports_signing:
            raise NotImplementedError("this storage backend does not support presigned URLs")
        return await obstore.sign_async(self._store, "GET", key, timedelta(seconds=expires_in))


def build_storage(settings: StorageSettings) -> Storage:
    backend = settings.backend
    if backend == "local":
        root = resolve_path(settings.url)
        root.mkdir(parents=True, exist_ok=True)
        # LocalStore requires an existing directory path as a string.
        return ObstoreStorage(LocalStore(str(root)), supports_signing=False)
    if backend == "s3":
        return ObstoreStorage(_build_s3(settings), supports_signing=True)
    if backend == "azure":
        return ObstoreStorage(_build_azure(settings), supports_signing=True)
    if backend == "gcs":
        return ObstoreStorage(_build_gcs(settings), supports_signing=True)
    raise ValueError(f"unknown storage backend: {backend!r}")


def _build_s3(s: StorageSettings) -> S3Store:
    """Construct an S3Store from settings.

    `s.url` must be an ``s3://bucket[/prefix]`` URL. Credentials fall back to the
    standard AWS environment variables / instance profile if not set explicitly.
    """
    kwargs: dict[str, Any] = {}
    if s.region:
        kwargs["region"] = s.region
    if s.access_key_id:
        kwargs["access_key_id"] = s.access_key_id
    if s.secret_access_key:
        kwargs["secret_access_key"] = s.secret_access_key
    if s.endpoint:
        kwargs["endpoint"] = s.endpoint
        if s.endpoint.startswith("http://"):
            kwargs["client_options"] = {"allow_http": True}
    return S3Store.from_url(s.url, **kwargs)


def _build_azure(s: StorageSettings) -> AzureStore:
    """Construct an AzureStore from settings.

    `s.url` should be ``az://container[/prefix]``. Credentials:
    - `access_key_id` maps to Azure storage account name
    - `secret_access_key` maps to account access key
    If either is empty, obstore falls back to `AZURE_STORAGE_*` env vars and
    default credential chain (managed identity / CLI).
    """
    kwargs: dict[str, Any] = {}
    if s.access_key_id:
        kwargs["account_name"] = s.access_key_id
    if s.secret_access_key:
        kwargs["access_key"] = s.secret_access_key
    return AzureStore.from_url(s.url, **kwargs)


def _build_gcs(s: StorageSettings) -> GCSStore:
    """Construct a GCSStore from settings.

    `s.url` should be ``gs://bucket[/prefix]``. Credentials come from
    `GOOGLE_APPLICATION_CREDENTIALS` or instance metadata by default.
    """
    kwargs: dict[str, Any] = {}
    if s.secret_access_key:
        # Treated as inline service-account JSON; obstore also accepts a path via env.
        kwargs["service_account_key"] = s.secret_access_key
    return GCSStore.from_url(s.url, **kwargs)


_storage: Storage | None = None


def get_storage() -> Storage:
    from conda_server.config import get_settings

    global _storage
    if _storage is None:
        _storage = build_storage(get_settings().storage)
    return _storage


def reset_storage() -> None:
    global _storage
    _storage = None


def set_storage(storage: Storage) -> None:
    """Explicit override — for tests."""
    global _storage
    _storage = storage
