"""Repairing records that stopped describing the bytes they name.

Reconciling *which* files exist is what the rest of a reindex does. It is
structurally blind to the one case where the filename never moved but the
archive underneath it did: CI that rebuilds a package under a stable
version+build-string produces a genuinely different hash under an
unchanged name, and every pass after that leaves the record alone because
the file is already listed. What the client sees is repodata advertising
a hash the download cannot reproduce.

So the properties here are about the *bytes*, not the listing:

- A record whose stored object changed size is rewritten, for free,
  during a routine pass.
- A steady-state channel still costs no archive reads.
- Drift the size gate cannot see needs ``verify``, and is found by it.

Archives are the real ``.conda`` files ``test_reindex_footprint`` builds,
because repair reads them through rattler and a fake would only prove the
test's own assumptions.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from conda_server.db import get_sessionmaker
from conda_server.indexer import (
    _reindex_via_metadata,
    _repair_drifted_records,
    _sync_db_from_repodata,
    reindex_channel,
)
from conda_server.models import Channel, PackageVersion

from .test_reindex_footprint import _ARCHIVE_PAYLOAD, make_archive, watched  # noqa: F401

_FILENAME = "pkg-a-1.0.0-h0.conda"


async def _seed_indexed_channel(storage, name: str) -> Channel:
    """One published package, with repodata and rows agreeing about it."""
    await storage.put(f"{name}/linux-64/{_FILENAME}", make_archive("1.0.0", payload_size=4096))

    sm = get_sessionmaker()
    async with sm() as session:
        channel = Channel(name=name, storage_prefix=name)
        session.add(channel)
        await session.commit()
        await _reindex_via_metadata(session, storage, channel)
        await _sync_db_from_repodata(session, storage, channel)
        await session.commit()
        return channel


async def _row(filename: str = _FILENAME) -> PackageVersion:
    sm = get_sessionmaker()
    async with sm() as session:
        result = await session.execute(
            PackageVersion.__table__.select().where(PackageVersion.filename == filename)
        )
        return result.first()


async def _published_record(storage, prefix: str) -> dict:
    raw = await storage.get(f"{prefix}/linux-64/repodata.json")
    return json.loads(raw)["packages.conda"][_FILENAME]


@pytest.mark.asyncio
async def test_repair_rewrites_a_record_whose_archive_was_replaced(app, watched):  # noqa: F811
    """The bug this exists for: same filename, different bytes.

    Both the row and the published record have to end up describing what
    is in storage now — a client reads the record, and everything the
    server renders reads the row.
    """
    storage, _ = watched
    channel = await _seed_indexed_channel(storage, "drift")

    replacement = make_archive("1.0.0", payload_size=_ARCHIVE_PAYLOAD)
    await storage.put(f"drift/linux-64/{_FILENAME}", replacement)
    actual_sha = hashlib.sha256(replacement).hexdigest()

    sm = get_sessionmaker()
    async with sm() as session:
        channel = await session.merge(channel)
        repaired = await _repair_drifted_records(session, storage, channel, verify=False)
        await session.commit()

    assert repaired == 1

    record = await _published_record(storage, "drift")
    assert record["sha256"] == actual_sha
    assert record["size"] == len(replacement)

    row = await _row()
    assert row.sha256 == actual_sha
    assert row.size == len(replacement)


@pytest.mark.asyncio
async def test_repair_reads_nothing_when_the_channel_is_intact(app, watched):  # noqa: F811
    """The default pass has to be free, or it cannot be the default.

    Sizes come out of the listing, so a channel whose objects match its
    records opens no archives at all.
    """
    storage, _ = watched
    channel = await _seed_indexed_channel(storage, "intact")

    storage.fetched_keys.clear()
    sm = get_sessionmaker()
    async with sm() as session:
        channel = await session.merge(channel)
        repaired = await _repair_drifted_records(session, storage, channel, verify=False)
        await session.commit()

    assert repaired == 0
    assert storage.archive_fetches == []


@pytest.mark.asyncio
async def test_verify_finds_drift_the_size_gate_cannot_see(app, watched):  # noqa: F811
    """A rebuild that keeps the length is invisible until asked for.

    The wrong hash is written onto the row directly rather than by
    producing two archives of identical length: what is under test is
    which records the gate lets through, and a compressor that happened
    to round to the same size would be testing zstd instead.
    """
    storage, _ = watched
    channel = await _seed_indexed_channel(storage, "samesize")

    stored = await storage.get(f"samesize/linux-64/{_FILENAME}")
    actual_sha = hashlib.sha256(stored).hexdigest()

    sm = get_sessionmaker()
    async with sm() as session:
        row = (
            await session.execute(
                PackageVersion.__table__.select().where(PackageVersion.filename == _FILENAME)
            )
        ).first()
        await session.execute(
            PackageVersion.__table__.update()
            .where(PackageVersion.id == row.id)
            .values(sha256="0" * 64, info={**row.info, "sha256": "0" * 64})
        )
        await session.commit()

    async with sm() as session:
        channel = await session.merge(channel)
        storage.fetched_keys.clear()
        assert await _repair_drifted_records(session, storage, channel, verify=False) == 0
        assert storage.archive_fetches == []

        repaired = await _repair_drifted_records(session, storage, channel, verify=True)
        await session.commit()

    assert repaired == 1
    assert (await _row()).sha256 == actual_sha
    assert (await _published_record(storage, "samesize"))["sha256"] == actual_sha


@pytest.mark.asyncio
async def test_repair_drops_the_stale_shard_index(app, watched):  # noqa: F811
    """pixi asks for the sharded index first, and nothing here can rewrite it.

    Leaving it beside a corrected repodata.json serves the hash we just
    disproved to the clients quickest to ask for one.
    """
    storage, _ = watched
    channel = await _seed_indexed_channel(storage, "shardy")
    await storage.put("shardy/linux-64/repodata_shards.msgpack.zst", b"stale-shard-index")

    await storage.put(
        f"shardy/linux-64/{_FILENAME}", make_archive("1.0.0", payload_size=_ARCHIVE_PAYLOAD)
    )

    sm = get_sessionmaker()
    async with sm() as session:
        channel = await session.merge(channel)
        await _repair_drifted_records(session, storage, channel, verify=False)
        await session.commit()

    assert await storage.head("shardy/linux-64/repodata_shards.msgpack.zst") is None


@pytest.mark.asyncio
async def test_reindex_channel_reports_what_it_repaired(app, watched, monkeypatch):  # noqa: F811
    """The repair pass is part of a reindex, not a separate command.

    ``_reindex_local`` is stubbed out so this is about the wiring rather
    than about rattler-index: the point is that a plain reindex now ends
    with the channel's hashes true, and says how many it had to fix.
    """
    storage, _ = watched
    channel = await _seed_indexed_channel(storage, "wired")

    replacement = make_archive("1.0.0", payload_size=_ARCHIVE_PAYLOAD)
    await storage.put(f"wired/linux-64/{_FILENAME}", replacement)

    async def _noop(settings, chan):
        return None

    monkeypatch.setattr("conda_server.indexer._reindex_local", _noop)

    sm = get_sessionmaker()
    async with sm() as session:
        channel = await session.merge(channel)
        outcome = await reindex_channel(session, storage, channel)
        await session.commit()

    assert outcome.repaired == 1
    assert (await _row()).sha256 == hashlib.sha256(replacement).hexdigest()
