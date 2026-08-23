"""Background maintenance sweeps that run inside the API pod.

Two sweeps, both driven by ``CleanupSettings`` and both independently
disableable:

* **Import job prune** — TTL delete of terminal ``ImportJob`` rows so the
  table doesn't accumulate one row per import forever.
* **About backfill trickle** — opens a small, fixed number of archives
  per channel per tick to fill in ``info/about.json`` metadata for
  versions indexed before that metadata was captured. Off by default,
  because unlike every other sweep here it *downloads package archives*
  and therefore costs bandwidth; see ``CleanupSettings``.

The trickle exists so a deployment can heal itself without anyone
remembering to press a button, and it is rate-limited by construction:
the per-tick cap bounds a single sweep, and the sweep interval bounds
the rate. It shares its runner with the admin-triggered job and the CLI,
so the "already inspected" stamp means the three can never duplicate
each other's work.

Anything that touches storage lifecycle (orphan blobs, abandoned S3
multipart uploads) is intentionally left to the storage backend's own
rules — see deploying.md for the bucket-level config.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select

from conda_server.backfill import backfill_about_batch
from conda_server.config import get_settings
from conda_server.db import get_sessionmaker
from conda_server.logging import get_logger
from conda_server.models import Channel, ImportJob
from conda_server.storage import get_storage

log = get_logger(__name__)


async def prune_import_jobs(*, max_age: timedelta) -> int:
    """Delete terminal ImportJob rows older than ``max_age``.

    Only ``completed`` and ``failed`` rows are touched — anything in
    ``pending`` or ``running`` is left alone (the lifespan startup hook
    sweeps stale runners to ``failed`` separately, so a row in either
    transient state is genuinely live).
    """
    cutoff = datetime.now(UTC) - max_age
    sm = get_sessionmaker()
    async with sm() as session:
        result = await session.execute(
            delete(ImportJob).where(
                ImportJob.status.in_(["completed", "failed"]),
                ImportJob.finished_at.is_not(None),
                ImportJob.finished_at < cutoff,
            )
        )
        await session.commit()
        return result.rowcount or 0


async def trickle_about_backfill(*, per_channel: int) -> int:
    """Open up to ``per_channel`` un-inspected archives in each channel.

    Mirror channels are skipped: they proxy an upstream and never
    materialise version rows, so there is nothing local to inspect.

    Each channel gets its own session and its own error boundary — one
    channel with unreachable storage must not stop the others from
    making progress, and a channel deleted mid-sweep is not an error.
    Returns the number of rows inspected across all channels.
    """
    sm = get_sessionmaker()
    storage = get_storage()
    total = 0

    async with sm() as session:
        channels = list(
            (await session.execute(select(Channel).where(Channel.mirror_url.is_(None)))).scalars()
        )

    for channel in channels:
        try:
            async with sm() as session:
                fresh = await session.get(Channel, channel.id)
                if fresh is None:
                    continue
                stats = await backfill_about_batch(session, storage, fresh, limit=per_channel)
            total += stats.inspected
            if stats.touched:
                log.info(
                    "cleanup.about_backfill",
                    channel=channel.name,
                    inspected=stats.inspected,
                    with_metadata=stats.with_metadata,
                    failed=stats.failed,
                    more_pending=stats.hit_limit,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("cleanup.about_backfill_failed", channel=channel.name)

    return total


async def cleanup_loop() -> None:
    """Run all enabled sweeps on the cleanup interval, forever.

    Started by the FastAPI lifespan as an asyncio task. Each sweep is
    wrapped in try/except so a transient DB hiccup doesn't kill the
    loop — the next tick will retry. If every sweep is disabled the
    loop exits rather than waking up hourly to do nothing.
    """
    cfg = get_settings().cleanup
    prune_enabled = cfg.import_job_ttl_days > 0
    backfill_enabled = cfg.about_backfill_per_sweep > 0

    if not prune_enabled and not backfill_enabled:
        log.info("cleanup.disabled", reason="no sweep enabled")
        return
    if not prune_enabled:
        log.info("cleanup.prune_disabled", reason="import_job_ttl_days<=0")

    interval = max(60, cfg.interval_seconds)
    ttl = timedelta(days=cfg.import_job_ttl_days)
    while True:
        if prune_enabled:
            try:
                n = await prune_import_jobs(max_age=ttl)
                if n:
                    log.info(
                        "cleanup.import_jobs_pruned",
                        rows=n,
                        older_than_days=cfg.import_job_ttl_days,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("cleanup.sweep_failed")

        if backfill_enabled:
            try:
                await trickle_about_backfill(per_channel=cfg.about_backfill_per_sweep)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("cleanup.sweep_failed")

        await asyncio.sleep(interval)
