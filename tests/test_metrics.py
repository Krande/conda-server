"""Smoke test for the Prometheus /metrics endpoint.

Verifies the scrape target responds with the expected content type, that
all declared metric families are registered (prometheus-client keeps
them in the registry after first observation), and that our upload path
actually moves the upload counter. Don't assert on raw values — tests
run in arbitrary order and the counter is process-global.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from conda_server import storage as storage_module
from conda_server.db import get_sessionmaker
from conda_server.models import Channel, User
from conda_server.storage import LocalStore, ObstoreStorage
from tests.conftest import make_session_cookie


async def _seed_admin() -> User:
    sm = get_sessionmaker()
    async with sm() as session:
        user = User(subject="metric-admin", email="m@x", username="m", role="admin")
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def _seed_channel(name: str) -> Channel:
    sm = get_sessionmaker()
    async with sm() as session:
        channel = Channel(name=name, storage_prefix=name)
        session.add(channel)
        await session.commit()
        await session.refresh(channel)
        return channel


@pytest.mark.asyncio
async def test_metrics_endpoint_serves_openmetrics_text(app, client):
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    # prometheus_client's default exposition format — text/plain with an
    # OpenMetrics-compatible body. Check it looks right without pinning
    # the exact version string.
    assert resp.headers["content-type"].startswith("text/plain")
    body = resp.text
    # All metric families declared in conda_server.metrics should be
    # present (prometheus_client emits # HELP lines on every scrape).
    for family in [
        "conda_server_mirror_upstream_fetches_total",
        "conda_server_mirror_upstream_bytes_total",
        "conda_server_mirror_cache_hits_total",
        "conda_server_uploads_total",
        "conda_server_upload_bytes_total",
        "conda_server_package_deletes_total",
        "conda_server_reindex_runs_total",
        "conda_server_reindex_duration_seconds",
    ]:
        assert family in body, f"missing metric family: {family}"


@pytest.mark.asyncio
async def test_upload_increments_upload_counters(app, client, tmp_path):
    admin = await _seed_admin()
    await _seed_channel("metrics-ch")
    cookie = make_session_cookie(admin.subject)

    store = ObstoreStorage(LocalStore(str(tmp_path)), supports_signing=False)
    storage_module.set_storage(store)
    try:

        class _Idx:
            subdir = "noarch"
            name = "m"
            version = "1.0"
            build = "0"

        before = await client.get("/metrics")
        body = b"X" * 4096
        files = [("files", ("m-1.0-0.conda", body, "application/octet-stream"))]
        with (
            patch(
                "conda_server.api.channels.rattler.IndexJson.from_package_archive",
                return_value=_Idx(),
            ),
            patch("conda_server.api.channels._reindex_background"),
        ):
            up = await client.post(
                "/api/channels/metrics-ch/packages",
                files=files,
                cookies={"session": cookie},
            )
        assert up.status_code == 202

        after = await client.get("/metrics")
        needle = 'conda_server_uploads_total{channel="metrics-ch",subdir="noarch"}'
        assert needle in after.text, "upload counter never appeared"
        # And the series wasn't there before (or had a lower value).
        assert after.text.count(needle) >= 1
        assert needle not in before.text or (
            # Same label set can appear on multiple scrapes — compare values.
            _series_value(before.text, needle) < _series_value(after.text, needle)
        )
    finally:
        storage_module.reset_storage()


def _series_value(metrics_text: str, needle: str) -> float:
    """Parse `needle value` out of the Prometheus exposition text."""
    for line in metrics_text.splitlines():
        if line.startswith(needle + " "):
            return float(line.rsplit(" ", 1)[-1])
    return 0.0
