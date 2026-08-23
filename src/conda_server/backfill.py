"""Populate ``info/about.json`` metadata for versions already indexed.

The indexer opens an archive only for a version it just added or whose
bytes just changed (see ``conda_server.indexer``), which keeps a routine
reindex from downloading the whole channel. The cost of that bound is
that versions indexed before metadata capture existed stay blank
forever: nothing about them ever changes, so nothing ever re-reads them.

This module is the deliberate pass that opens the rest. It is shared by
three callers so they cannot drift:

* the ``backfill-about`` CLI command,
* the admin-triggered background job behind the channel endpoints,
* the optional low-rate sweep in ``conda_server.cleanup``.

Two properties make it safe to run repeatedly from any of them:

* **Idempotent.** Every row inspected is stamped with
  ``about_fetched_at`` whether or not the archive had an ``about.json``,
  so a second pass skips it instead of paying for the download again.
  Rows whose *fetch* failed are left unstamped on purpose — a transient
  storage error should be retried, not remembered as "no metadata".
* **Resumable at batch granularity.** Progress is committed every
  ``_COMMIT_EVERY`` rows rather than once at the end, so a run killed
  partway keeps the archives it already paid to download. That matters
  more than it looks: these runs are long, and the whole point of the
  stamp is defeated if a restart discards it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from conda_server.indexer import capture_about
from conda_server.logging import get_logger
from conda_server.models import Channel, Package, PackageVersion
from conda_server.storage import Storage

log = get_logger(__name__)

#: Rows to process before committing. Small enough that a killed run
#: loses only a few archive reads, large enough that the commit itself
#: is not the dominant cost of a pass.
_COMMIT_EVERY = 25

#: Archives are streamed to a temporary file on local disk before their
#: metadata member is read, so N workers can hold up to N times the
#: indexer's archive size cap on disk at once. The limit on parallelism
#: here is therefore free disk, not CPU or bandwidth, and containers
#: often run with a modest ephemeral-storage allowance. Two is a
#: conservative default that still overlaps network waits; raise it only
#: if you know the host has room.
DEFAULT_CONCURRENCY = 2


@dataclass
class BackfillStats:
    """Outcome of one pass. Counts rows, not bytes."""

    #: Rows whose archive was opened (or skipped by the size cap) and
    #: stamped. These will not be revisited.
    inspected: int = 0
    #: Subset of ``inspected`` that yielded at least one usable field.
    with_metadata: int = 0
    #: Rows whose archive could not be fetched. Left unstamped, so a
    #: later pass retries them.
    failed: int = 0
    #: True when the pass stopped because it hit ``limit`` rather than
    #: because it ran out of work — i.e. there is more to do.
    hit_limit: bool = False

    @property
    def touched(self) -> int:
        return self.inspected + self.failed


def _has_metadata(row: PackageVersion) -> bool:
    return any((row.doc_url, row.home, row.dev_url, row.summary, row.description))


async def count_pending(session: AsyncSession, channel: Channel) -> int:
    """How many of a channel's versions have never been inspected.

    Cheap enough to call before starting a job so the UI can show a
    real denominator instead of counting up from zero.
    """
    return (
        await session.scalar(
            select(func.count(PackageVersion.id))
            .join(Package, PackageVersion.package_id == Package.id)
            .where(
                Package.channel_id == channel.id,
                PackageVersion.about_fetched_at.is_(None),
            )
        )
        or 0
    )


async def backfill_about_batch(
    session: AsyncSession,
    storage: Storage,
    channel: Channel,
    *,
    limit: int,
    force: bool = False,
    concurrency: int = DEFAULT_CONCURRENCY,
    on_progress: Callable[[BackfillStats], Awaitable[None]] | None = None,
) -> BackfillStats:
    """Open up to ``limit`` of a channel's un-inspected archives.

    ``force`` re-reads rows that were already stamped, which is what you
    want after fixing the parser — otherwise the stamp correctly hides
    them. ``on_progress`` is awaited after each committed batch so a job
    row can be updated; it is not called per row, because that would put
    a database write in front of every archive read.

    Returns what the pass did. ``hit_limit`` tells the caller whether
    running again would find more work.
    """
    stmt = (
        select(PackageVersion)
        .join(Package, PackageVersion.package_id == Package.id)
        .where(Package.channel_id == channel.id)
        .order_by(PackageVersion.id)
        .limit(limit)
    )
    if not force:
        stmt = stmt.where(PackageVersion.about_fetched_at.is_(None))

    rows = list((await session.execute(stmt)).scalars())
    stats = BackfillStats(hit_limit=len(rows) == limit)
    if not rows:
        return stats

    prefix = channel.storage_prefix.strip("/")
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(row: PackageVersion) -> None:
        async with sem:
            # capture_about mutates the row in place; it never raises,
            # returning False for a fetch it could not complete.
            if await capture_about(storage, prefix, row):
                stats.inspected += 1
                if _has_metadata(row):
                    stats.with_metadata += 1
            else:
                stats.failed += 1

    # Chunked rather than one big gather: the commit boundary is what
    # makes the pass resumable, and gathering everything would put that
    # boundary at the end again.
    for start in range(0, len(rows), _COMMIT_EVERY):
        chunk = rows[start : start + _COMMIT_EVERY]
        await asyncio.gather(*(_one(row) for row in chunk))
        await session.commit()
        if on_progress is not None:
            await on_progress(stats)

    log.info(
        "about.backfill_batch",
        channel=channel.name,
        inspected=stats.inspected,
        with_metadata=stats.with_metadata,
        failed=stats.failed,
        hit_limit=stats.hit_limit,
    )
    return stats
