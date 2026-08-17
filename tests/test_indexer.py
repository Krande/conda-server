"""Indexer tests.

Two levels of coverage:

- ``_sync_db_from_repodata`` is tested directly against a hand-crafted
  ``repodata.json`` in storage, avoiding the cost and flakiness of a real
  ``rattler-index`` invocation.
- ``reindex_channel`` is smoke-tested end-to-end against a local filesystem
  backend with an empty (noarch-only) channel to confirm ``rattler.index``
  runs and produces a ``repodata.json`` we can read back.
"""

from __future__ import annotations

import json
import os

import pytest

from conda_server import storage as storage_module
from conda_server.config import StorageSettings, reset_settings
from conda_server.db import get_sessionmaker
from conda_server.indexer import _sync_db_from_repodata, reindex_channel
from conda_server.models import Channel, Package, PackageVersion
from conda_server.storage import build_storage


def _fake_repodata(packages: dict[str, dict]) -> dict:
    return {
        "info": {"subdir": "linux-64"},
        "packages": {},
        "packages.conda": packages,
        "repodata_version": 2,
    }


@pytest.mark.asyncio
async def test_sync_adds_new_packages(app, tmp_path):
    storage = build_storage(StorageSettings(backend="local", url=str(tmp_path)))
    storage_module.set_storage(storage)
    try:
        repodata = _fake_repodata(
            {
                "numpy-1.26.0-py312h1.conda": {
                    "name": "numpy",
                    "version": "1.26.0",
                    "build": "py312h1",
                    "build_number": 1,
                    "size": 12345,
                    "sha256": "a" * 64,
                    "md5": "b" * 32,
                    "depends": ["python >=3.12"],
                    "constrains": [],
                    "timestamp": 1700000000000,
                },
            }
        )
        await storage.put(
            "chan1/linux-64/repodata.json",
            json.dumps(repodata).encode(),
        )

        sm = get_sessionmaker()
        async with sm() as session:
            channel = Channel(name="chan1", storage_prefix="chan1")
            session.add(channel)
            await session.commit()

            stats = await _sync_db_from_repodata(session, storage, channel)
            await session.commit()

        assert stats == {"added": 1, "updated": 0, "removed": 0}

        async with sm() as session:
            pkg = (
                await session.execute(Package.__table__.select().where(Package.name == "numpy"))
            ).first()
            assert pkg is not None
            ver = (
                await session.execute(
                    PackageVersion.__table__.select().where(
                        PackageVersion.filename == "numpy-1.26.0-py312h1.conda"
                    )
                )
            ).first()
            assert ver is not None
            assert ver.version == "1.26.0"
            assert ver.size == 12345
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_sync_updates_and_removes(app, tmp_path):
    storage = build_storage(StorageSettings(backend="local", url=str(tmp_path)))
    storage_module.set_storage(storage)
    try:
        # First pass: two packages.
        first = _fake_repodata(
            {
                "a-1.0-0.conda": {
                    "name": "a",
                    "version": "1.0",
                    "build": "0",
                    "build_number": 0,
                    "size": 10,
                    "sha256": "a" * 64,
                    "md5": "a" * 32,
                    "depends": [],
                    "constrains": [],
                },
                "b-2.0-0.conda": {
                    "name": "b",
                    "version": "2.0",
                    "build": "0",
                    "build_number": 0,
                    "size": 20,
                    "sha256": "b" * 64,
                    "md5": "b" * 32,
                    "depends": [],
                    "constrains": [],
                },
            }
        )
        await storage.put("c/linux-64/repodata.json", json.dumps(first).encode())

        sm = get_sessionmaker()
        async with sm() as session:
            channel = Channel(name="c", storage_prefix="c")
            session.add(channel)
            await session.commit()
            await _sync_db_from_repodata(session, storage, channel)
            await session.commit()

        # Second pass: a updated (new size), b removed, c added.
        second = _fake_repodata(
            {
                "a-1.0-0.conda": {
                    "name": "a",
                    "version": "1.0",
                    "build": "0",
                    "build_number": 0,
                    "size": 99,
                    "sha256": "A" * 64,
                    "md5": "a" * 32,
                    "depends": [],
                    "constrains": [],
                },
                "cc-3.0-0.conda": {
                    "name": "cc",
                    "version": "3.0",
                    "build": "0",
                    "build_number": 0,
                    "size": 30,
                    "sha256": "c" * 64,
                    "md5": "c" * 32,
                    "depends": [],
                    "constrains": [],
                },
            }
        )
        await storage.put("c/linux-64/repodata.json", json.dumps(second).encode())

        async with sm() as session:
            channel = (
                await session.execute(Channel.__table__.select().where(Channel.name == "c"))
            ).first()
            channel_obj = await session.get(Channel, channel.id)
            stats = await _sync_db_from_repodata(session, storage, channel_obj)
            await session.commit()

        assert stats == {"added": 1, "updated": 1, "removed": 1}
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_reindex_empty_local_channel_runs_rattler(app, tmp_path, monkeypatch):
    """Smoke test: rattler.index.index_fs produces a repodata.json for an empty channel."""
    monkeypatch.setenv("CONDA_SERVER_STORAGE__URL", str(tmp_path))
    reset_settings()

    storage = build_storage(StorageSettings(backend="local", url=str(tmp_path)))
    storage_module.set_storage(storage)
    try:
        sm = get_sessionmaker()
        async with sm() as session:
            channel = Channel(name="empty", storage_prefix="empty")
            session.add(channel)
            await session.commit()

            result = await reindex_channel(session, storage, channel)
            await session.commit()

        assert result.added == 0
        assert result.removed == 0

        # rattler-index must have written a repodata.json under noarch at minimum.
        generated = tmp_path / "empty" / "noarch" / "repodata.json"
        assert generated.exists(), "rattler-index did not produce noarch/repodata.json"
        body = json.loads(generated.read_text())
        assert body.get("info", {}).get("subdir") == "noarch"
    finally:
        storage_module.reset_storage()
        # Ensure we don't leak the storage URL env var into the next test.
        os.environ.pop("CONDA_SERVER_STORAGE__URL", None)
        reset_settings()
