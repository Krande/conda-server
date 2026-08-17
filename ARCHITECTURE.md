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

### Reindex

1. `CronJob` (or admin action) calls `conda-server reindex <channel>`.
2. CLI lists the channel prefix in object storage via `obstore`.
3. For each new `.conda` / `.tar.bz2` file, `py-rattler` extracts `info/index.json` + related metadata.
4. Metadata upserted into `PackageVersion` rows.
5. `rattler-index` regenerates `repodata.json` + `repodata.json.zst` + `current_repodata.json`, uploaded back to storage.
6. `Channel.repodata_updated_at` bumped to invalidate caches.

## Data model (initial)

- `User` — id, subject (OIDC), email, role
- `ApiKey` — user_id, key_hash, description, expires_at
- `Channel` — name, description, private, storage_prefix, repodata_updated_at
- `Package` — channel_id, name, description
- `PackageVersion` — package_id, version, build, build_number, subdir, filename, sha256, md5, size, depends (json), constrains (json), timestamp

Since implemented (beyond this initial cut): per-channel member ACLs,
mirror/import-from-upstream channels, upload quotas, and an audit log —
see the `ChannelMember`, `AuditLog`, and `ImportJob` models.

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
  cli.py            Typer CLI (serve / reindex / migrate)
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
