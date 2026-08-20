# Deploying conda-server in production

This is a walk-through for operators standing up conda-server at an
organisation. It covers the moving parts beyond the dev quick-start in
the README — the things that bit me running it in production,
documented so the next person doesn't have to rediscover them.

This walk-through targets a Kubernetes deployment (GitOps-friendly,
S3-compatible storage, standalone Postgres). A packaged Helm chart ships
in [`deploy/helm/conda-server/`](../deploy/helm/conda-server/) — see the
[Helm chart](#helm-chart) section below.

## Architecture at a glance

```
                                          ┌─ OIDC provider (Authentik / Keycloak / …)
                                          ▼
client (pixi/conda/mamba) ──▶ reverse proxy (Traefik / nginx / …)
                                          │
                                          ▼
                                    conda-server pod
                                     ├─▶ Postgres (metadata: users, channels, packages)
                                     └─▶ Object storage (package bytes: S3 / Azure / GCS / local)
                                              │
                                              ▼
                                        presigned URL 302
                                              │
                                              ▼
                                      client downloads directly
                                     (object storage, not the pod)
```

Three dependencies you provide:

1. **Relational database.** SQLite works for dev; Postgres 16+ is the
   production path. App ships `alembic upgrade head` as the migration
   entrypoint — run it before the API comes up.
2. **Object storage.** Anything S3-compatible (AWS, Garage, MinIO,
   Ceph, Backblaze B2…), Azure Blob, GCS, or a plain local filesystem.
3. **OIDC provider.** Any issuer that speaks
   `/.well-known/openid-configuration`. See the README for the Authentik
   walkthrough.

## Environment variables

All settings accept nested env vars with the `CONDA_SERVER_` prefix and
`__` as the nesting delimiter. Defaults live in
[`config.py`](../src/conda_server/config.py).

| Variable | Default | Notes |
|---|---|---|
| `CONDA_SERVER_HOST` | `0.0.0.0` | |
| `CONDA_SERVER_PORT` | `8000` | |
| `CONDA_SERVER_BASE_URL` | `http://localhost:8000` | External URL, used for building OIDC redirect URIs and install-instructions. |
| `CONDA_SERVER_DATABASE__URL` | `sqlite+aiosqlite:///./conda-server.db` | `postgresql+asyncpg://…` in prod. |
| `CONDA_SERVER_STORAGE__BACKEND` | `local` | `local` / `s3` / `azure` / `gcs`. |
| `CONDA_SERVER_STORAGE__URL` | `./data` | `s3://bucket[/prefix]`, `az://container[/prefix]`, or a local path. |
| `CONDA_SERVER_STORAGE__ENDPOINT` | _unset_ | **Required for non-AWS S3.** See [S3-compatible storage](#s3-compatible-storage). |
| `CONDA_SERVER_STORAGE__REGION` | _unset_ | AWS region or the region the S3-compat implementation wants. `us-east-1` is a safe default for self-hosted. |
| `CONDA_SERVER_STORAGE__ACCESS_KEY_ID` | _unset_ | Omit to use the instance profile / env-var credential chain. |
| `CONDA_SERVER_STORAGE__SECRET_ACCESS_KEY` | _unset_ | |
| `CONDA_SERVER_STORAGE__PRESIGN_TTL_SECONDS` | `900` | Lifetime of download redirects. |
| `CONDA_SERVER_AUTH__SESSION_SECRET` | `change-me-in-production` | `openssl rand -hex 32`. |
| `CONDA_SERVER_AUTH__SESSION_HTTPS_ONLY` | `true` | Set `false` only for plain-HTTP local dev. |
| `CONDA_SERVER_AUTH__INITIAL_ADMINS` | `[]` | JSON list of emails promoted to `admin` on first login. |
| `CONDA_SERVER_AUTH__OIDC__ISSUER` | _unset_ | The discovery URL's parent — trailing slash matters if your IdP publishes it that way (Authentik does). |
| `CONDA_SERVER_AUTH__OIDC__CLIENT_ID` | _unset_ | |
| `CONDA_SERVER_AUTH__OIDC__CLIENT_SECRET` | _unset_ | |
| `CONDA_SERVER_UPLOAD__MAX_FILE_BYTES` | `1 GiB` | Per-file cap on admin uploads. |
| `CONDA_SERVER_UPLOAD__MAX_TOTAL_BYTES` | `4 GiB` | Per-request cap across all files. |
| `CONDA_SERVER_LOGGING__FORMAT` | `json` | `json` or `console`. |

TOML alternative via `conda-server.toml` (sibling to the CWD), see
[`conda-server.example.toml`](../conda-server.example.toml).

## Postgres

Standard connection-string URL with the **asyncpg** driver:

```
CONDA_SERVER_DATABASE__URL=postgresql+asyncpg://conda-server:<password>@db.example:5432/conda-server
```

Run migrations before the first API boot, and every upgrade after:

```bash
alembic upgrade head
```

In Kubernetes the canonical pattern is an **init container** that runs
`alembic upgrade head` with the same env as the main container; the API
only starts after migrations succeed. The example deployment pairs this
with a busybox `wait-for-db` init container so an out-of-order
Postgres/app sequence doesn't crashloop the pod.

**Assembling the URL safely**: keep the password as its own secret and
build the connection string with `$(VAR)` Kubernetes env substitution so
the raw secret never appears in a ConfigMap:

```yaml
- name: DB_PASSWORD
  valueFrom:
    secretKeyRef: { name: conda-server-secrets, key: db-password }
- name: CONDA_SERVER_DATABASE__URL
  value: "postgresql+asyncpg://conda-server:$(DB_PASSWORD)@conda-server-db:5432/conda-server"
```

Name the password-only var something that doesn't start with
`CONDA_SERVER_` — pydantic-settings parses every prefixed variable and
will reject `CONDA_SERVER_DATABASE__PASSWORD` as an unknown field.

## S3-compatible storage

AWS just works with the defaults. **Self-hosted S3 (Garage, MinIO,
Ceph)** has three gotchas that will cost you hours if you miss them.

### 1. Path-style addressing

`rattler-index` defaults to virtual-host-style addressing, building URLs
like `https://<bucket>.<endpoint>/…`. That works on AWS (it has a
wildcard `*.s3.region.amazonaws.com` cert) but fails on any endpoint
serving a single TLS cert for the endpoint host — the client connects
to `mybucket.garage-s3.example.com`, gets a cert for
`garage-s3.example.com`, and rustls correctly rejects it as "self-signed"
(mismatched CN).

The indexer is already hard-coded to path-style (`<endpoint>/<bucket>/`)
via `S3Credentials(addressing_style="path", …)`. Keep it that way unless
you're on AWS and want the cosmetic perf of virtual-host.

### 2. Browser CORS for the "Show files" view

The SPA lets you peek inside an archive without involving the server —
it downloads the `.conda`, unpacks it in the browser, and shows the
file list. That fetch crosses origins (`your-frontend` →
`your-s3-endpoint`) and needs CORS headers on the S3 response.

For Garage (the CLI has no CORS subcommand, so you drive
`PutBucketCors` via the S3 API):

```bash
cat > /tmp/cors.xml <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<CORSConfiguration>
  <CORSRule>
    <AllowedOrigin>https://conda.example.com</AllowedOrigin>
    <AllowedMethod>GET</AllowedMethod>
    <AllowedMethod>HEAD</AllowedMethod>
    <AllowedHeader>*</AllowedHeader>
    <ExposeHeader>Content-Length</ExposeHeader>
    <ExposeHeader>Content-Type</ExposeHeader>
    <ExposeHeader>Content-Disposition</ExposeHeader>
    <ExposeHeader>ETag</ExposeHeader>
    <MaxAgeSeconds>3600</MaxAgeSeconds>
  </CORSRule>
</CORSConfiguration>
EOF

curl -sS -X PUT \
  --aws-sigv4 "aws:amz:us-east-1:s3" \
  --user "$AK:$SK" \
  --data-binary @/tmp/cors.xml \
  -H "Content-Type: application/xml" \
  "https://garage-s3.example.com/my-bucket?cors"
```

The frontend uses `credentials: "same-origin"` for this flow, not
`include`, so `Access-Control-Allow-Credentials: true` is **not**
required on the S3 response — plain CORS is enough.

For MinIO: `mc anonymous set-public`/the `Access-Control-*` parts of its
Web Identity policy; see MinIO docs. For AWS: `aws s3api put-bucket-cors`.

**For Azure Blob (`storage.backend=azure`):** the redirect lands on the
`https://<account>.blob.core.windows.net/...` SAS URL, which is always a
different origin than the app host, so the "Show files" fetch is blocked
until the account has a CORS rule. Unlike S3, Azure Blob CORS is a
**blob-service (account-level)** property — there is no per-container
CORS — so a single rule covers every container:

```bash
az storage cors add \
  --services b \
  --methods GET HEAD \
  --origins https://conda.example.com \
  --allowed-headers '*' \
  --exposed-headers 'Content-Length,Content-Type,Content-Disposition,ETag' \
  --max-age 3600 \
  --account-name <storage-account> \
  --account-key "$AZURE_STORAGE_KEY"   # or --auth-mode login
```

`az storage cors add` appends, so re-running piles up duplicate rules;
`az storage cors clear --services b` first if you want a clean replace.
The Helm chart's `blobCors` Job automates exactly this (clear-then-add)
as a post-install hook — set `blobCors.enabled=true`,
`blobCors.accountName`, and `blobCors.allowedOrigin`.

### 3. Content-Type / Content-Disposition at PUT time

Browsers MIME-sniff `application/octet-stream` responses, and a `.conda`
is just a zip — so a direct download from S3 without a
`Content-Disposition: attachment; filename="…"` header ends up being
saved as `foo.zip`. The app sets both headers as object attributes at
PUT time (admin uploads and mirror cache writes both), which S3 persists
and returns on every subsequent GET. No action needed from the operator
— but if you see `.conda` files downloading as `.zip`, this is why.

### 4. Aborting incomplete multipart uploads

`obstore` writes objects via S3 multipart uploads. If an import is
killed mid-PUT (pod OOM, network drop, container restart), the parts
that were already uploaded stay in the bucket counting against your
storage quota — they aren't visible as objects but they're billed.

Configure the bucket to abort stale multiparts itself rather than
relying on an app-level cron. Garage 1.0+ and AWS S3 both support
the same `AbortIncompleteMultipartUpload` lifecycle rule:

```bash
cat > lifecycle.json <<'JSON'
{
  "Rules": [{
    "ID": "abort-stale-multiparts",
    "Status": "Enabled",
    "Filter": {"Prefix": ""},
    "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1}
  }]
}
JSON

aws --endpoint-url https://your-s3-endpoint s3api \
  put-bucket-lifecycle-configuration \
  --bucket conda-server --lifecycle-configuration file://lifecycle.json
```

One day is well past any legitimate import duration. The app does **not**
attempt to abort multiparts itself — there's no obstore API for it and
the bucket-level rule is a one-time config that survives pod restarts.

## rustls, system CA bundles, and SSL_CERT_FILE

obstore (our S3 client) uses rustls, which reads system CA bundles from
the OS default paths on Linux. The production Dockerfile is based on
`debian:bookworm-slim` but skips `apt-get install ca-certificates` for
CI-reliability reasons (the build runner doesn't always have outbound
HTTP). Instead the image ships the pixi environment's CA bundle, and
the deployment sets:

```yaml
- name: SSL_CERT_FILE
  value: "/app/.pixi/envs/default/ssl/cacert.pem"
```

If you're rolling your own Dockerfile and seeing
`InvalidCertificate(UnknownIssuer)` on any HTTPS call out of the pod,
this is the knob.

## Reverse proxy / ingress

uvicorn must run with `--proxy-headers --forwarded-allow-ips='*'` so
that it honours the `X-Forwarded-Proto` header and `request.url_for()`
returns `https://…` URLs. The shipped Dockerfile already does this;
custom CMDs need to preserve it, otherwise OIDC's `redirect_uri` check
will fail ("redirect_uri_mismatch") because the server thinks it's
serving plain HTTP.

If the ingress host resolves to an address that isn't reachable from
inside the cluster (e.g. `authentik.example.com` points at the ingress
controller's external IP via a split-horizon or private DNS setup), add
a `hostAliases` entry to the pod spec pointing the hostname at the
ingress controller's cluster IP. Otherwise the server can't reach the
OIDC issuer for discovery.

## NetworkPolicy pitfalls (k8s)

If you lock down pods with NetworkPolicy, a few non-obvious egress
targets matter:

- **Kubernetes API**: the `kube-proxy` iptables rules DNAT the
  `10.43.0.1:443` (or whatever your service CIDR gives) to the actual
  control-plane endpoint *before* NetworkPolicy evaluates. So an
  egress rule for `ipBlock: 10.43.0.1/32` won't match — write it
  against the real endpoint IP/port (`kubectl get endpoints -n default
  kubernetes`). Relevant for any sidecar/job that talks to the API,
  including k8up backup pods.
- **Object storage via public ingress**: if your S3 endpoint is
  TLS-terminated at the cluster's ingress controller, egress from the
  app pod to the ingress pod on the websecure port (often `:8443`, not
  `:443` — NetworkPolicy matches pod ports, not service ports).

## Backups

**Postgres**: nightly `pg_dump` is the minimum viable path. The example
deployment uses [k8up](https://k8up.io) with a `PreBackupPod` that runs
`pg_dump | restic backup --stdin` at 03:15 UTC, prunes weekly, and
verifies the restic repo monthly. Wire the k8up `Schedule` / `PreBackupPod`
manifests into your own gitops repo for the full setup, including the
NetworkPolicy entries the backup pod needs.

**Object storage**: rarely backed up directly — mirror-cached bytes are
re-pullable from upstream, and the canonical source for uploaded bytes
is whoever did the upload. If your object-store implementation doesn't
do replication for you, add a separate restic-based backup job.

**Restore drill**: an untested restore isn't really a backup. A
minimally-useful drill:

```bash
# 1. Restore the latest snapshot into a temp directory.
restic -r $RESTIC_REPO restore latest --target /tmp/restore

# 2. Check it decoded to SQL.
head /tmp/restore/conda-server-conda-server-db-dump.sql

# 3. Restore into a scratch DB to prove it replays.
psql -h db -U postgres -c "CREATE DATABASE conda_server_drill;"
psql -h db -U postgres -d conda_server_drill < /tmp/restore/conda-server-conda-server-db-dump.sql
psql -h db -U postgres -d conda_server_drill -c "SELECT count(*) FROM channels;"
```

Good cadence: once per quarter against a junior operator, with the
handbook open.

## Observability

`/metrics` is a Prometheus text-format endpoint with domain metrics:
mirror upstream fetches (by channel + kind), cache hits, admin uploads
/ deletes (by channel), reindex outcomes + duration. Labels are
bounded to finite enums so cardinality stays sane. Add it to your
Prometheus config with either a `ServiceMonitor` (if you run the
operator) or pod-discovery annotations:

```yaml
annotations:
  prometheus.io/scrape: "true"
  prometheus.io/port: "8000"
  prometheus.io/path: "/metrics"
```

Worthwhile alerts (not shipped yet):

- **Backup Job failed in the last 36h** — obvious.
- **Reindex error rate > 0 over 15m** — the background task can fail
  silently; the metric is the canary.
- **Mirror upstream fetch failure rate** — upstream degradation or
  misconfigured credentials.
- **Pod restart count > N in 1h** — catch OOMs + crashloops.

## Rolling updates

Once the app is backed by an externalised database and object storage
(i.e. the pod carries no state), a `RollingUpdate` strategy gives
zero-downtime deploys:

```yaml
strategy:
  type: RollingUpdate
  rollingUpdate:
    maxSurge: 1
    maxUnavailable: 0
```

The pod is stateless; scale it horizontally if you need to.

## Debugging checklist

Symptoms you'll probably see at some point, and what they usually mean:

| Symptom | Likely cause |
|---|---|
| `InvalidCertificate(UnknownIssuer)` on any outbound HTTPS | `SSL_CERT_FILE` not pointing at a CA bundle the container can read. |
| `.conda` downloads saved as `.zip` | Object stored without `Content-Disposition` — re-upload, or set the header via storage CLI. |
| "Failed to fetch" / "blocked by CORS policy" on the browser file-view ("Show files"), or the app's in-UI "download was blocked by the browser" hint | Missing CORS rule on the object storage. See [Troubleshooting: browser file listing (CORS)](#troubleshooting-browser-file-listing-cors) for the per-backend fix (S3 / Azure Blob / GCS). |
| Reindex silently "completes" but the DB doesn't reflect the new upload | Check for `S3Credentials -> Mapping` conversion errors in the pod logs. |
| `unknown subdir` on a page refresh of a deep URL like `/channels/foo/packages/bar` | The SPA fallback route isn't ordered after the repodata route. Shouldn't happen with the shipped app; verify nothing reordered it. |
| OIDC `redirect_uri_mismatch` | uvicorn not started with `--proxy-headers`, or the `BASE_URL` env doesn't match the hostname the IdP sees. |
| Pod can't reach its own S3 endpoint | If the endpoint resolves to a private IP outside the cluster, add `hostAliases` pointing the hostname at your ingress controller. |

## Troubleshooting: browser file listing (CORS)

**Symptom.** In the package view, clicking **Show files** fails. The app
shows a hint like *"Couldn't load files — the download was blocked by the
browser"* and links here; the browser devtools console shows a
`TypeError: Failed to fetch` and/or a *"blocked by CORS policy"* warning.

**Cause.** "Show files" downloads the `.conda` and unpacks it *in the
browser* — the server isn't involved in the parse. The download endpoint
302-redirects that fetch to a **presigned URL on your object storage**
(S3 / Azure Blob / GCS), which is a different origin than the app. Unless
the bucket/account has a CORS rule allowing your site's origin, the
browser blocks the response before JavaScript can read it. Browsers
deliberately hide whether a failure was CORS or a plain network error, so
the app can only heuristically suggest CORS — but a missing rule is by far
the most common cause.

The fix is a one-time CORS rule on the storage backend. In every case the
**allowed origin is the app's own origin** (the `scheme://host[:port]` you
load the UI from — the app fills this into its hint automatically; use the
same value below). Allow `GET` and `HEAD`. Local-filesystem backends serve
bytes through the app itself (same origin) and need no CORS rule — a
failure there is a genuine network/server issue, not CORS.

Which backend you run is shown on the **About** page (and in the
`/api/about` response as `storage_backend`).

### S3 / S3-compatible (`storage.backend=s3`)

See [Browser CORS for the "Show files" view](#2-browser-cors-for-the-show-files-view)
above for the full Garage / MinIO / AWS walkthrough. In short: apply a
bucket CORS rule via `PutBucketCors` (`aws s3api put-bucket-cors`, or the
raw S3 API for Garage) allowing `GET`/`HEAD` from your origin.

### Azure Blob (`storage.backend=azure`)

Azure Blob CORS is an **account-level** (blob-service) property — one rule
covers every container. See the
[Azure Blob steps](#2-browser-cors-for-the-show-files-view) above:

```bash
az storage cors add \
  --services b --methods GET HEAD \
  --origins https://conda.example.com \
  --allowed-headers '*' \
  --exposed-headers 'Content-Length,Content-Type,Content-Disposition,ETag' \
  --max-age 3600 \
  --account-name <storage-account> --account-key "$AZURE_STORAGE_KEY"
```

### Google Cloud Storage (`storage.backend=gcs`)

GCS CORS is a **per-bucket** property, set from a JSON file:

```bash
cat > cors.json <<'JSON'
[
  {
    "origin": ["https://conda.example.com"],
    "method": ["GET", "HEAD"],
    "responseHeader": ["Content-Length", "Content-Type", "Content-Disposition", "ETag"],
    "maxAgeSeconds": 3600
  }
]
JSON

# Modern gcloud:
gcloud storage buckets update gs://my-bucket --cors-file=cors.json
# Or legacy gsutil:
gsutil cors set cors.json gs://my-bucket
```

Verify with `gcloud storage buckets describe gs://my-bucket --format='default(cors_config)'`
(or `gsutil cors get gs://my-bucket`).

As with S3, this flow uses `credentials: "same-origin"`, so
`Access-Control-Allow-Credentials` is **not** required on the storage
response — a plain origin allow-list is enough.

## Helm chart

A chart lives at [`deploy/helm/conda-server/`](../deploy/helm/conda-server/).
See its [README](../deploy/helm/conda-server/README.md) for the install
walkthroughs (dev, self-hosted prod, hyperscaler-managed) and the
required-values cheat sheet. It bundles every gotcha from this doc:
optional Postgres StatefulSet, optional CORS post-install Job, optional
NetworkPolicy, optional k8up resources, optional PrometheusRule for the
domain alerts, and a pre-install migration Job that runs `alembic
upgrade head` before the API pod boots.

Garage / MinIO themselves remain out of scope — pre-provision the
bucket with your existing tooling, then let the chart's `bucketCors`
Job attach the CORS rule. The Helm chart is the recommended install
path; wire it into whatever GitOps workflow you already run.
