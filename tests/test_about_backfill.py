"""The backfill pass that fills in metadata the indexer deliberately skips.

The indexer only opens an archive for a version it just added or whose
bytes just changed, so versions indexed before metadata capture existed
stay blank forever. These tests cover the three things that make the
backfill a safe way to fix that:

* it is **idempotent** — the ``about_fetched_at`` stamp means a second
  pass skips what the first one read, so re-running costs nothing;
* it is **resumable** — progress is committed as it goes, so a run that
  dies partway keeps the archives it already paid to download;
* it does **not** relax the indexer's cost bound, which is a separate
  guarantee tested in ``test_package_metadata``.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import select

from conda_server import storage as storage_module
from conda_server.backfill import backfill_about_batch, count_pending
from conda_server.config import StorageSettings
from conda_server.db import get_sessionmaker
from conda_server.models import Channel, MaintenanceJob, Package, PackageVersion, User
from conda_server.storage import build_storage
from tests.conftest import make_session_cookie
from tests.test_package_about import FULL_ABOUT, make_conda

DOCS = "https://example.com/docs/pkg-a/"


class _FlakyStorage:
    """Counts archive reads and can be told to abort partway through.

    ``cancel_after`` makes the next stream call raise
    ``asyncio.CancelledError``. That is the realistic way one of these
    runs dies: a plain storage error is deliberately *not* fatal
    (``capture_about`` swallows it and moves on), so the thing that
    actually stops a pass mid-flight is the task being cancelled —
    a restart, a shutdown, a Ctrl-C.
    """

    def __init__(self, inner):
        self._inner = inner
        self.stream_calls: list[str] = []
        self.cancel_after: int | None = None

    def __getattr__(self, item):
        return getattr(self._inner, item)

    def stream(self, key: str):
        self.stream_calls.append(key)
        if self.cancel_after is not None and len(self.stream_calls) > self.cancel_after:
            raise asyncio.CancelledError()
        return self._inner.stream(key)


def _repodata(entries: dict[str, int]) -> bytes:
    return json.dumps(
        {
            "info": {"subdir": "linux-64"},
            "packages": {},
            "packages.conda": {
                filename: {
                    "name": "pkg-a",
                    "version": filename.split("-")[2],
                    "build": "h0",
                    "build_number": 0,
                    "subdir": "linux-64",
                    "sha256": "a" * 64,
                    "size": size,
                    "depends": [],
                }
                for filename, size in entries.items()
            },
            "repodata_version": 2,
        }
    ).encode()


async def _seed_channel(tmp_path, count: int, name: str = "example"):
    """A channel with ``count`` real archives in storage and blank rows.

    The rows deliberately have no ``about_fetched_at``: this is exactly
    the state a channel is left in after upgrading to a server that
    reads metadata, since nothing about the existing artifacts changed.
    """
    storage = _FlakyStorage(build_storage(StorageSettings(backend="local", url=str(tmp_path))))
    storage_module.set_storage(storage)

    sm = get_sessionmaker()
    async with sm() as session:
        channel = Channel(name=name, storage_prefix=name)
        session.add(channel)
        await session.flush()
        pkg = Package(channel_id=channel.id, name="pkg-a")
        session.add(pkg)
        await session.flush()

        entries: dict[str, int] = {}
        for i in range(count):
            filename = f"pkg-a-1.{i}.0-h0.conda"
            archive = make_conda(tmp_path / filename, FULL_ABOUT)
            payload = archive.read_bytes()
            await storage.put(f"{name}/linux-64/{filename}", payload)
            entries[filename] = len(payload)
            session.add(
                PackageVersion(
                    package_id=pkg.id,
                    version=f"1.{i}.0",
                    build="h0",
                    build_number=0,
                    subdir="linux-64",
                    filename=filename,
                    size=len(payload),
                )
            )
        await storage.put(f"{name}/linux-64/repodata.json", _repodata(entries))
        await session.commit()
        await session.refresh(channel)
        return storage, channel


@pytest.mark.asyncio
async def test_backfill_fills_blank_rows(app, tmp_path):
    storage, channel = await _seed_channel(tmp_path, 3)
    try:
        sm = get_sessionmaker()
        async with sm() as session:
            chan = await session.get(Channel, channel.id)
            assert await count_pending(session, chan) == 3
            stats = await backfill_about_batch(session, storage, chan, limit=10)

        assert stats.inspected == 3
        assert stats.with_metadata == 3
        assert stats.failed == 0
        assert stats.hit_limit is False

        async with sm() as session:
            rows = list((await session.execute(PackageVersion.__table__.select())).all())
            assert len(rows) == 3
            assert all(r.doc_url == DOCS for r in rows)
            assert all(r.about_fetched_at is not None for r in rows)
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_second_pass_reads_nothing(app, tmp_path):
    """The stamp is what makes re-running free — and it must hold."""
    storage, channel = await _seed_channel(tmp_path, 3)
    try:
        sm = get_sessionmaker()
        async with sm() as session:
            chan = await session.get(Channel, channel.id)
            await backfill_about_batch(session, storage, chan, limit=10)

        reads_after_first = len(storage.stream_calls)
        assert reads_after_first == 3

        async with sm() as session:
            chan = await session.get(Channel, channel.id)
            assert await count_pending(session, chan) == 0
            stats = await backfill_about_batch(session, storage, chan, limit=10)

        assert stats.touched == 0
        assert len(storage.stream_calls) == reads_after_first, "second pass must read nothing"
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_limit_bounds_a_run_and_reports_more_work(app, tmp_path):
    storage, channel = await _seed_channel(tmp_path, 5)
    try:
        sm = get_sessionmaker()
        async with sm() as session:
            chan = await session.get(Channel, channel.id)
            stats = await backfill_about_batch(session, storage, chan, limit=2)

        assert stats.inspected == 2
        assert stats.hit_limit is True
        assert len(storage.stream_calls) == 2, "limit must bound egress, not just the report"

        async with sm() as session:
            chan = await session.get(Channel, channel.id)
            assert await count_pending(session, chan) == 3
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_force_reopens_already_stamped_rows(app, tmp_path):
    """What you need after fixing the parser: the stamp must be overridable."""
    storage, channel = await _seed_channel(tmp_path, 2)
    try:
        sm = get_sessionmaker()
        async with sm() as session:
            chan = await session.get(Channel, channel.id)
            await backfill_about_batch(session, storage, chan, limit=10)

        async with sm() as session:
            chan = await session.get(Channel, channel.id)
            stats = await backfill_about_batch(session, storage, chan, limit=10, force=True)

        assert stats.inspected == 2
        assert len(storage.stream_calls) == 4
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_interrupted_run_keeps_the_archives_it_already_read(app, tmp_path):
    """The point of committing incrementally rather than once at the end.

    A run that is cancelled partway — a restart, a shutdown — used to
    discard every stamp in the batch, so the next run re-downloaded
    archives that had already been read and parsed. Pure waste, and
    unbounded if the restart recurred. Progress must survive.
    """
    from conda_server.backfill import _COMMIT_EVERY

    # More rows than one commit chunk, so cancellation lands after at
    # least one chunk has been committed.
    count = _COMMIT_EVERY + 5
    storage, channel = await _seed_channel(tmp_path, count)
    try:
        sm = get_sessionmaker()
        storage.cancel_after = _COMMIT_EVERY

        with pytest.raises(asyncio.CancelledError):
            async with sm() as session:
                chan = await session.get(Channel, channel.id)
                await backfill_about_batch(session, storage, chan, limit=count)

        async with sm() as session:
            chan = await session.get(Channel, channel.id)
            remaining = await count_pending(session, chan)

        assert remaining == count - _COMMIT_EVERY, (
            "the first committed chunk must survive the cancellation"
        )

        # And a fresh run resumes rather than starting over.
        storage.cancel_after = None
        reads_before = len(storage.stream_calls)
        async with sm() as session:
            chan = await session.get(Channel, channel.id)
            stats = await backfill_about_batch(session, storage, chan, limit=count)

        assert stats.inspected == remaining
        assert len(storage.stream_calls) - reads_before == remaining, (
            "the resumed run must not re-read what the first run committed"
        )
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_unreadable_objects_do_not_abort_the_pass(app, tmp_path):
    """One bad object must not cost the whole run.

    These passes are long and run unattended. Aborting on the first
    unreadable archive would mean a single corrupt or deleted object
    blocks every version behind it, forever.
    """
    storage, channel = await _seed_channel(tmp_path, 4)
    try:
        sm = get_sessionmaker()
        async with sm() as session:
            rows = list((await session.execute(PackageVersion.__table__.select())).all())
            pv = await session.get(PackageVersion, rows[0].id)
            pv.filename = "absent-9.9.9-h0.conda"
            await session.commit()

            chan = await session.get(Channel, channel.id)
            stats = await backfill_about_batch(session, storage, chan, limit=10)

        assert stats.failed == 1
        assert stats.inspected == 3, "the other three must still be read"
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_fetch_failure_leaves_the_row_for_a_later_pass(app, tmp_path):
    """A missing object is transient — it must not be stamped as "no metadata"."""
    storage, channel = await _seed_channel(tmp_path, 1)
    try:
        sm = get_sessionmaker()
        async with sm() as session:
            chan = await session.get(Channel, channel.id)
            # Point the row at an object that isn't there.
            row = (await session.execute(PackageVersion.__table__.select())).first()
            pv = await session.get(PackageVersion, row.id)
            pv.filename = "absent-9.9.9-h0.conda"
            await session.commit()

            stats = await backfill_about_batch(session, storage, chan, limit=10)

        assert stats.failed == 1
        assert stats.inspected == 0

        async with sm() as session:
            chan = await session.get(Channel, channel.id)
            assert await count_pending(session, chan) == 1, "must be retried later"
    finally:
        storage_module.reset_storage()


# --- the admin-triggered job ------------------------------------------


async def _seed_admin(subject: str = "admin1", email: str = "a@example.com") -> User:
    sm = get_sessionmaker()
    async with sm() as session:
        user = User(subject=subject, email=email, username="admin", role="admin")
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


@pytest.mark.asyncio
async def test_endpoint_runs_a_job_to_completion(app, client, tmp_path):
    from conda_server.api.channels import _RUNNING_BACKFILLS

    _storage, channel = await _seed_channel(tmp_path, 2)
    try:
        admin = await _seed_admin()
        cookie = make_session_cookie(admin.subject)

        started = await client.post(
            f"/api/channels/{channel.name}/backfill-about", cookies={"session": cookie}
        )
        assert started.status_code == 202, started.text
        body = started.json()
        assert body["pending"] == 2
        job_id = body["job_id"]

        # Let the background runner finish before asking about it, rather
        # than polling while it works. The suite runs on an in-memory
        # SQLite database where every session shares a single connection,
        # so a concurrent request would interleave its transaction with
        # the runner's; a real deployment hands each session its own.
        await asyncio.gather(*list(_RUNNING_BACKFILLS))

        got = await client.get(
            f"/api/channels/{channel.name}/backfill-about/jobs/{job_id}",
            cookies={"session": cookie},
        )
        assert got.status_code == 200
        job = got.json()
        assert job["status"] == "completed", job
        assert job["completed_count"] == 2
        assert job["with_metadata_count"] == 2
        assert job["failed_count"] == 0
        assert job["kind"] == "about_backfill"
        assert job["finished_at"] is not None

        # And the metadata actually reached the package page.
        pkg = await client.get(f"/api/channels/{channel.name}/packages/pkg-a")
        assert pkg.json()["doc_url"] == DOCS
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_endpoint_reports_up_to_date_without_creating_a_job(app, client, tmp_path):
    """Nothing pending must not manufacture a job row to poll."""
    storage, channel = await _seed_channel(tmp_path, 1)
    try:
        sm = get_sessionmaker()
        async with sm() as session:
            chan = await session.get(Channel, channel.id)
            await backfill_about_batch(session, storage, chan, limit=10)

        admin = await _seed_admin()
        res = await client.post(
            f"/api/channels/{channel.name}/backfill-about",
            cookies={"session": make_session_cookie(admin.subject)},
        )

        assert res.status_code == 202
        assert res.json() == {
            "status": "up-to-date",
            "channel": channel.name,
            "pending": 0,
            "job_id": None,
        }
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_second_job_is_refused_while_one_is_running(app, client, tmp_path):
    """Two passes would select overlapping rows and pay for the same bytes twice."""
    _storage, channel = await _seed_channel(tmp_path, 1)
    try:
        admin = await _seed_admin()
        cookie = make_session_cookie(admin.subject)

        sm = get_sessionmaker()
        async with sm() as session:
            session.add(
                MaintenanceJob(
                    kind="about_backfill",
                    channel_id=channel.id,
                    user_id=admin.id,
                    status="running",
                )
            )
            await session.commit()

        res = await client.post(
            f"/api/channels/{channel.name}/backfill-about", cookies={"session": cookie}
        )
        assert res.status_code == 409
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_mirror_channel_is_rejected(app, client, tmp_path):
    _storage, channel = await _seed_channel(tmp_path, 1, name="mirrored")
    try:
        sm = get_sessionmaker()
        async with sm() as session:
            chan = await session.get(Channel, channel.id)
            chan.mirror_url = "https://upstream.example.com/channel"
            await session.commit()

        admin = await _seed_admin()
        res = await client.post(
            "/api/channels/mirrored/backfill-about",
            cookies={"session": make_session_cookie(admin.subject)},
        )
        assert res.status_code == 400
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_endpoint_requires_owner(app, client, tmp_path):
    _storage, channel = await _seed_channel(tmp_path, 1)
    try:
        sm = get_sessionmaker()
        async with sm() as session:
            session.add(User(subject="nobody", email="n@example.com", role="user"))
            await session.commit()

        anon = await client.post(f"/api/channels/{channel.name}/backfill-about")
        assert anon.status_code in (401, 403)

        regular = await client.post(
            f"/api/channels/{channel.name}/backfill-about",
            cookies={"session": make_session_cookie("nobody")},
        )
        assert regular.status_code == 403
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_startup_sweep_fails_jobs_stranded_by_a_restart(app, tmp_path):
    """An in-process runner can't survive a restart, so the row must not lie."""
    from conda_server.app import _fail_orphaned_jobs

    _storage, channel = await _seed_channel(tmp_path, 1)
    try:
        sm = get_sessionmaker()
        async with sm() as session:
            session.add(
                MaintenanceJob(kind="about_backfill", channel_id=channel.id, status="running")
            )
            await session.commit()

        await _fail_orphaned_jobs()

        async with sm() as session:
            job = (await session.execute(select(MaintenanceJob))).scalar_one()
            assert job.status == "failed"
            assert job.error
            assert job.finished_at is not None
    finally:
        storage_module.reset_storage()


# --- the opt-in trickle sweep -----------------------------------------


@pytest.mark.asyncio
async def test_trickle_is_bounded_per_channel(app, tmp_path):
    """The whole safety property of the automatic sweep is its cap."""
    from conda_server.cleanup import trickle_about_backfill

    storage, channel = await _seed_channel(tmp_path, 5)
    try:
        inspected = await trickle_about_backfill(per_channel=2)
        assert inspected == 2
        assert len(storage.stream_calls) == 2

        sm = get_sessionmaker()
        async with sm() as session:
            chan = await session.get(Channel, channel.id)
            assert await count_pending(session, chan) == 3
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_trickle_skips_mirror_channels(app, tmp_path):
    from conda_server.cleanup import trickle_about_backfill

    storage, channel = await _seed_channel(tmp_path, 2)
    try:
        sm = get_sessionmaker()
        async with sm() as session:
            chan = await session.get(Channel, channel.id)
            chan.mirror_url = "https://upstream.example.com/channel"
            await session.commit()

        assert await trickle_about_backfill(per_channel=10) == 0
        assert storage.stream_calls == []
    finally:
        storage_module.reset_storage()


@pytest.mark.asyncio
async def test_trickle_is_off_by_default(app):
    """Nobody should discover a bandwidth bill by upgrading."""
    from conda_server.config import CleanupSettings

    assert CleanupSettings().about_backfill_per_sweep == 0
