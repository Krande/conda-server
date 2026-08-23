"""Storage wrapper smoke tests.

Uses obstore's MemoryStore so the suite runs without network access or cloud
credentials. The ObstoreStorage wrapper is intentionally a thin passthrough —
we exercise every method once to guard against obstore API drift.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from obstore.store import LocalStore, MemoryStore

from conda_server.config import StorageSettings
from conda_server.storage import ObstoreStorage, build_storage


@pytest.fixture
def mem_storage() -> ObstoreStorage:
    return ObstoreStorage(MemoryStore(), supports_signing=False)


@pytest.mark.asyncio
async def test_put_get_roundtrip(mem_storage: ObstoreStorage):
    await mem_storage.put("a/b.conda", b"hello")
    assert await mem_storage.get("a/b.conda") == b"hello"


@pytest.mark.asyncio
async def test_delete(mem_storage: ObstoreStorage):
    await mem_storage.put("x", b"1")
    await mem_storage.delete("x")
    with pytest.raises(FileNotFoundError):
        await mem_storage.get("x")


@pytest.mark.asyncio
async def test_list_returns_keys_and_sizes(mem_storage: ObstoreStorage):
    await mem_storage.put("foo/one", b"aa")
    await mem_storage.put("foo/two", b"bbb")
    await mem_storage.put("bar/three", b"c")

    foo_keys = [m.key async for m in mem_storage.list("foo/")]
    assert sorted(foo_keys) == ["foo/one", "foo/two"]

    all_metas = [m async for m in mem_storage.list("")]
    sizes = {m.key: m.size for m in all_metas}
    assert sizes == {"foo/one": 2, "foo/two": 3, "bar/three": 1}


@pytest.mark.asyncio
async def test_get_range_returns_exactly_the_requested_slice(mem_storage: ObstoreStorage):
    await mem_storage.put("a.bin", bytes(range(256)))

    assert await mem_storage.get_range("a.bin", start=10, length=5) == bytes(range(10, 15))


@pytest.mark.asyncio
async def test_get_range_past_the_end_returns_the_remainder(mem_storage: ObstoreStorage):
    """HTTP range semantics. A caller sizing its last read from ``head``
    would otherwise have to get the arithmetic exactly right or fail."""
    await mem_storage.put("a.bin", b"0123456789")

    assert await mem_storage.get_range("a.bin", start=7, length=999) == b"789"


@pytest.mark.asyncio
async def test_get_range_of_nothing_is_not_a_request(mem_storage: ObstoreStorage):
    """obstore rejects a zero-length range outright, and a caller
    computing a length from two offsets can legitimately arrive at zero."""
    await mem_storage.put("a.bin", b"0123456789")

    assert await mem_storage.get_range("a.bin", start=0, length=0) == b""


@pytest.mark.asyncio
async def test_get_range_on_a_missing_object_raises(mem_storage: ObstoreStorage):
    with pytest.raises(FileNotFoundError):
        await mem_storage.get_range("absent.bin", start=0, length=4)


@pytest.mark.asyncio
async def test_get_range_on_the_local_backend(tmp_path: Path):
    """The filesystem backend serves ranges too — a local deployment
    reads package metadata through the same path a cloud one does."""
    storage = build_storage(StorageSettings(backend="local", url=str(tmp_path)))
    await storage.put("dir/a.bin", bytes(range(256)))

    assert await storage.get_range("dir/a.bin", start=200, length=8) == bytes(range(200, 208))


@pytest.mark.asyncio
async def test_presign_unsupported_raises(mem_storage: ObstoreStorage):
    with pytest.raises(NotImplementedError):
        await mem_storage.presign_get("any", expires_in=60)


@pytest.mark.asyncio
async def test_build_storage_local(tmp_path: Path):
    storage = build_storage(StorageSettings(backend="local", url=str(tmp_path)))
    await storage.put("dir/file.txt", b"payload")
    assert await storage.get("dir/file.txt") == b"payload"
    # Local backend does not sign.
    with pytest.raises(NotImplementedError):
        await storage.presign_get("dir/file.txt", expires_in=60)


def test_build_storage_s3_requires_url():
    # Construction must not silently succeed with an unparseable URL.
    with pytest.raises(ValueError):
        build_storage(StorageSettings(backend="s3", url="not-a-url"))


def test_build_storage_unknown_backend_rejected():
    with pytest.raises(ValueError):
        # Bypass pydantic Literal validation by constructing manually.
        s = StorageSettings(backend="local", url="./data")
        s.__dict__["backend"] = "nope"
        build_storage(s)


@pytest.mark.asyncio
async def test_obstore_localstore_direct(tmp_path: Path):
    """Sanity check that obstore's LocalStore works inside our wrapper."""
    storage = ObstoreStorage(LocalStore(str(tmp_path)), supports_signing=False)
    await storage.put("a.txt", b"x")
    assert await storage.get("a.txt") == b"x"
