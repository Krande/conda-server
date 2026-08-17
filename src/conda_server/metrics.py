"""Prometheus metrics for the conda server.

Focus is on the domain: mirror cache hit/miss/bytes, admin upload/delete
counts, reindex outcomes. Generic HTTP metrics (per-path request counts)
aren't exposed here — reverse-proxy logs + access logs cover that without
polluting this instrumentation.

Labels are kept to values whose cardinality we control: channel name
(handful per cluster), subdir (finite enum), `kind` (finite enum). No
filename labels — those would unbound the label space.
"""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.responses import Response

# ---- mirror (proxy-cache) metrics ------------------------------------------

MIRROR_UPSTREAM_FETCHES = Counter(
    "conda_server_mirror_upstream_fetches_total",
    "Count of upstream fetches initiated by a mirror channel (successes + failures).",
    labelnames=("channel", "kind", "result"),
)

MIRROR_UPSTREAM_BYTES = Counter(
    "conda_server_mirror_upstream_bytes_total",
    "Bytes streamed from upstream into object storage for a mirror channel.",
    labelnames=("channel", "kind"),
)

MIRROR_CACHE_HITS = Counter(
    "conda_server_mirror_cache_hits_total",
    "Mirror requests served from the local cache without touching upstream.",
    labelnames=("channel", "kind"),
)

# ---- admin / content-management metrics ------------------------------------

UPLOADS_TOTAL = Counter(
    "conda_server_uploads_total",
    "Admin package uploads accepted.",
    labelnames=("channel", "subdir"),
)

UPLOAD_BYTES = Counter(
    "conda_server_upload_bytes_total",
    "Bytes accepted through the admin upload endpoint.",
    labelnames=("channel",),
)

PACKAGE_DELETES = Counter(
    "conda_server_package_deletes_total",
    "Admin package-version deletions.",
    labelnames=("channel", "subdir"),
)

# ---- reindex metrics -------------------------------------------------------

REINDEX_RUNS = Counter(
    "conda_server_reindex_runs_total",
    "Reindex runs against a channel, tagged by outcome.",
    labelnames=("channel", "result"),
)

REINDEX_DURATION = Histogram(
    "conda_server_reindex_duration_seconds",
    "Wall-clock duration of a single reindex run.",
    labelnames=("channel",),
    # Buckets biased toward the expected range: fast DB-only syncs
    # (seconds) up through full index_s3 scans on a conda-forge mirror
    # (tens of minutes). Everything past 1h lands in +Inf.
    buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600, 1800, 3600),
)


async def metrics_endpoint() -> Response:
    """Starlette/FastAPI-compatible handler that renders the default registry."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
