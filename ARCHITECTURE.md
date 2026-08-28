# Architecture

A modern, general-purpose conda server built on the [rattler](https://github.com/conda/rattler) ecosystem. Serves `repodata.json` and conda packages, with pluggable object storage and OIDC auth. Intended to be simple to run (single container, SQLite by default) and horizontally scalable (Postgres + object storage + CDN).

## Design principles

1. **Don't serve bytes through Python.** Packages and `repodata.json` are served from object storage via HTTP redirects (presigned URLs) or CDN — Python only handles metadata, auth, and control plane.
2. **Use the fast tools.** `py-rattler` for conda metadata parsing and indexing, `obstore` for object storage, `rattler-index` for `repodata.json` generation.
3. **Simple default, scalable ceiling.** SQLite + local filesystem runs out of the box; swap to Postgres + S3/Azure without code changes.
4. **Stateless API pods.** All state is in the database or object storage. Reindex runs as a k8s `CronJob` or admin-triggered task, not a long-lived worker.

## Stack

| Concern          | Choice                                               | Why                                                     |
| ---------------- | ---------------------------------------------------- | ------------------------------------------------------- |
| Web framework    | FastAPI + uvicorn                                    | Ecosystem, async, py-rattler compatibility              |
| ORM / migrations | SQLAlchemy 2.0 async + Alembic                       | Industry standard; same code works on SQLite & Postgres |
| Database         | SQLite (dev) / Postgres (prod)                       | Simple default, painless scale-up                       |
| Object storage   | obstore (S3 / Azure / GCS / local)                   | Rust-backed, typed, fast                                |
| Conda tooling    | py-rattler + rattler-index                           | Replaces conda-build + manual repodata generation       |
| Auth             | authlib (OIDC) + bearer tokens                       | Works with GitHub / Google / Azure AD / generic OIDC    |
| Jobs             | k8s CronJob (MVP); arq later if needed               | No Redis dependency for the common case                 |
| Observability    | structlog (JSON) + prometheus_client + OTel-optional | Cloud-native defaults                                   |
| Config           | pydantic-settings (env + TOML)                       | 12-factor, with local-dev ergonomics                    |
| Frontend         | React 19 + Vite 6 + Tailwind 4 + TS                  | Modern SPA stack, fast HMR                               |
| Packaging        | pixi (conda-forge) + hatchling (pip)                 | Reproducible builds, pip-installable                    |
| Deploy           | Multi-stage Docker → OCI registry → k8s (gitops)     | Portable, registry-agnostic                             |

## Request flow

### `GET /{channel}/{subdir}/repodata.json[.zst]`

1. Look up `Channel` in DB, check access.
2. Return 302 redirect to object storage (optionally presigned) or stream directly if the channel is small.
3. `Cache-Control: public, max-age=…` + `ETag` from the DB row's `repodata_updated_at`.

### `GET /{channel}/{subdir}/{filename}.conda`

1. Validate the filename exists in `PackageVersion`, check access.
2. Issue presigned URL (S3/Azure) with short TTL, return 302.
3. Record a download metric (async, non-blocking).

### `POST /api/channels/{channel}/packages` (upload)

1. Spool each archive to a temp file (one at a time), reading its size,
   sha256 and md5 as the bytes go past.
2. `py-rattler` reads `info/index.json` from the spooled file; the declared
   subdir decides where the artifact is stored. `info/about.json` is read from
   the same file, while it is still local and therefore free.
3. Stream the archive into object storage.
4. Merge the new record into the subdir's `repodata.json` (+ `.zst`) and upsert
   the `PackageVersion` row.
5. **Only then** respond.

Step 5 is the point. Updating the index from a task scheduled to run *after* the
response makes a 2xx a prediction rather than a fact: the artifact is in object
storage and the index is not, so a crash in between leaves a package that was
accepted, is paid for, and cannot be installed — and nothing in the response
distinguishes that from a publish that worked. Cost is the size of the affected
subdir's index, not of the channel, which is what makes it affordable to do
before answering.

### Reindex

1. `CronJob` (or admin action) calls `conda-server reindex <channel>`.
2. CLI lists the channel prefix in object storage via `obstore`.
3. `repodata.json` + `repodata.json.zst` are regenerated. How depends on the
   backend, and the difference is only where the archives already are:
   - **local** — `rattler-index`'s `index_fs` over the path; the files are
     already on disk, so nothing is copied.
   - **s3** — `index_s3`, which works against the bucket in place.
   - **everything else** — no in-place indexer exists, so the index is built
     from archive metadata directly (`_reindex_via_metadata`).
4. Metadata upserted into `PackageVersion` rows.
5. Records whose stored object no longer matches them are re-hashed and
   rewritten (`_repair_drifted_records`).
6. `Channel.repodata_updated_at` bumped to invalidate caches.

**Reindexing must not scale in disk with the size of the channel.** The
tempting implementation of the generic route — mirror every artifact into a
temp directory and point `index_fs` at it — holds the whole channel on
ephemeral disk, which is a fixed per-pod quota shared with everything else in
the container and one the process cannot grow. So the generic route reads
metadata instead: a package already in the database contributes its stored
record and is never fetched at all (a steady-state reindex moves zero bytes),
and one the database does not know is spooled, read, and deleted before the
next is considered. The disk high-water mark is one archive.

#### Records that stopped describing their bytes

Steps 1–4 reconcile *which* files exist. They are blind to the case where the
filename never moved but the archive under it did — a CI job that rebuilds a
package under a stable version+build-string, where the build is not
byte-reproducible. `rattler-index` leaves a filename already present in
repodata alone, so the superseded hash survives every subsequent pass and the
run reports no changes at all, while clients fail to extract what they
downloaded.

Step 5 closes that. The storage listing already carries every object's size,
so one free pass catches any rebuild whose output changed length, and only the
suspects are re-read. Bytes replaced without the length changing need
`reindex --verify`, which drops the size gate and re-hashes the channel — the
cost is the channel's egress, which is why it is an explicit operator decision
rather than the price of a routine reindex.

The upload path publishes the record for the bytes it just stored, replacing
any record for the same filename, so it does not create this state; a crash
between the object write and the index write does, and this is the way back.

#### Package metadata (`info/about.json`)

`repodata.json` is built from `info/index.json` and carries none of the
human-facing metadata a package page wants — documentation URL, homepage,
repository, summary, description. Those live in a different archive member,
`info/about.json`, so they have to be read from the artifact itself.

Cost bound, in two parts. **Which packages**: only those with an artifact the
pass just added, or whose bytes just changed — a reindex of an unchanged channel
reads zero archives. **Which of their versions**: only the newest by conda
ordering, because `_about_source` in the packages API renders no other, so
metadata captured for an older version is metadata nothing asks for. "Newest" is
recomputed from the package's whole version list on every pass, so a rebuild of
an older version landing after a newer one shipped is not mistaken for the
newest, and a version that becomes the newest is captured then. The
import-from-upstream path reads its own metadata from the copy it already
spooled locally, so it costs no extra fetch at all. So does the upload path.

Reading one archive costs a fraction of the archive, not the archive. A
`.conda` is a zip and a zip keeps its index at the tail, so `head` plus two
ranged reads — the tail window, then the `info-*.tar.zst` member it points at —
reach the metadata without the artifact leaving object storage and without
anything touching local disk. Measured against conda-forge packages that is
~0.5–1% of the bytes, for a package of any size. Legacy `.tar.bz2` archives have
no index — one solid bz2 stream — so there is nothing to seek to and they are
still spooled to a temporary file; `indexer.MAX_ABOUT_ARCHIVE_BYTES` bounds that
path and only that path, skipping and stamping anything above it.

Those bounds mean some rows stay blank: versions indexed before this existed,
and versions that are not the newest. Inspecting them is a separate, explicit
pass (`conda_server.backfill`) that covers **every** version rather than just
the newest — which is also what keeps `_about_source`'s older-version fallback
meaningful. It is available three ways that all share one runner and one safety
property:

- **Channel admin page** — a "Backfill package metadata" button that starts a
  background `MaintenanceJob` and reports progress while it runs. The usual
  way to do it.
- **`conda-server backfill-about <channel>`** — the same pass from the CLI.
- **`cleanup.about_backfill_per_sweep`** — an opt-in trickle that opens a few
  archives per channel on each cleanup tick, so a deployment heals without
  anyone pressing anything. **Off by default**: unlike every other sweep it
  reads package archives out of storage, which costs bandwidth.

The safety property is the `about_fetched_at` stamp: every row inspected is
stamped whether or not the archive had an `about.json`, so the three can never
duplicate each other's work and any of them can be re-run for free. Progress is
committed as it goes, so a pass that is interrupted keeps what it already read.

## Data model (initial)

- `User` — id, subject (OIDC), email, role
- `ApiKey` — user_id, key_hash, description, expires_at
- `Channel` — name, description, private, storage_prefix, repodata_updated_at
- `Package` — channel_id, name, description
- `PackageVersion` — package_id, version, build, build_number, subdir, filename, sha256, md5, size, depends (json), constrains (json), timestamp, plus the `info/about.json` fields (doc_url, home, dev_url, summary, description) and the `about_fetched_at` stamp

Since implemented (beyond this initial cut): per-channel member ACLs,
mirror/import-from-upstream channels, upload quotas, and an audit log —
see the `ChannelMember`, `AuditLog`, `ImportJob`, and `MaintenanceJob` models.

## Directory layout

```
src/conda_server/
  app.py            FastAPI factory
  config.py         pydantic-settings
  db.py             async engine + session
  models.py         SQLAlchemy 2.0 models
  storage.py        obstore wrapper
  indexer.py        py-rattler + rattler-index
  auth.py           authlib OIDC + bearer tokens
  cli.py            Typer CLI (serve / reindex / backfill-about / channel)
  backfill.py       shared info/about.json backfill pass (CLI, admin job, sweep)
  cleanup.py        periodic in-pod maintenance sweeps
  package_about.py  info/about.json extraction from .conda / .tar.bz2
  logging.py        structlog setup
  api/              routers (health, channels, repodata, packages, auth)
  migrations/       alembic
frontend/           React SPA
tests/              pytest
```

## Escape hatches

- Python web layer too slow? The `/repodata.json` endpoint can be moved to a Rust sidecar using rattler directly. API contract stays stable.
- Postgres too hot? Read replicas + PgBouncer, no code change.
- Need a task queue? `arq` (async Redis queue) is a one-file add.
- Need multi-region? Object storage + CDN handles reads; writes stay in one region.
