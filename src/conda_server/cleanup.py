"""Background maintenance sweeps that run inside the API pod.

Currently only one sweep: TTL prune of terminal ``ImportJob`` rows so
the table doesn't accumulate one row per import forever. Cadence and
retention come from ``CleanupSettings``.

Anything that touches storage (orphan blobs, abandoned S3 multipart
uploads) is intentionally left to the storage backend's own lifecycle
rules — see deploying.md for the bucket-level config.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete

from conda_server.config import get_settings
from conda_server.db import get_sessionmaker
from conda_server.logging import get_logger
from conda_server.models import ImportJob

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


async def cleanup_loop() -> None:
    """Run all configured sweeps on the cleanup interval, forever.

    Started by the FastAPI lifespan as an asyncio task. Each sweep is
    wrapped in try/except so a transient DB hiccup doesn't kill the
    loop — the next tick will retry.
    """
    cfg = get_settings().cleanup
    if cfg.import_job_ttl_days <= 0:
        log.info("cleanup.disabled", reason="import_job_ttl_days<=0")
        return
    interval = max(60, cfg.interval_seconds)
    ttl = timedelta(days=cfg.import_job_ttl_days)
    while True:
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
        await asyncio.sleep(interval)
