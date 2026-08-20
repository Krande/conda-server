# conda-server Helm chart

Packages the conda-server app + everything an operator typically wants
to deploy alongside it.

## Install scenarios

The chart is designed around three install shapes, in order of growing
sophistication.

### 1. Dev / kicking the tyres

Bundled Postgres, local-filesystem storage, no ingress, no OIDC. Fine
for `helm install` against any cluster (kind, k3d, k3s) just to see
the UI come up.

```bash
helm install dev ./deploy/helm/conda-server \
  --set postgresql.enabled=true \
  --set storage.backend=local \
  --set storage.url=/data \
  --set secrets.values.sessionSecret="$(openssl rand -hex 32)" \
  --set secrets.values.dbPassword="$(openssl rand -hex 16)" \
  --set app.sessionHttpsOnly=false \
  --set 'app.initialAdmins[0]=you@example.com'
```

A reference values file with the same shape lives at
[`ci/test-values.yaml`](./ci/test-values.yaml).

### 2. Self-hosted production

External Postgres + S3-compatible object storage (Garage / MinIO / B2)
+ OIDC + ingress + bucket CORS + alert rules + k8up backups. Designed
for a gitops workflow. See
[`ci/prod-values.yaml`](./ci/prod-values.yaml).

```bash
helm install prod ./deploy/helm/conda-server -f my-values.yaml
```

### 3. Hyperscaler-managed

External managed Postgres (RDS, CloudSQL, Aurora, …) + S3 (real AWS)
+ corporate OIDC. Drop `storage.endpoint` (defaults work), keep
`postgresql.enabled=false`, point `externalDatabase.url` at the
managed cluster, leave `bucketCors.enabled=false` (AWS S3 has its own
CORS UI / Terraform resource).

## Required values cheat-sheet

| Value                              | Required when                                   |
| ---------------------------------- | ----------------------------------------------- |
| `app.baseUrl`                      | always — used for OIDC redirect and UI snippets |
| `secrets.values.sessionSecret`     | unless `secrets.existingSecret` is set          |
| `postgresql.enabled` OR `externalDatabase.url` | always — pick exactly one          |
| `secrets.values.dbPassword`        | when bundled Postgres is enabled OR the external URL uses `$(DB_PASSWORD)` substitution |
| `storage.backend` + `storage.url`  | always                                          |
| `storage.endpoint`                 | when `backend=s3` and the endpoint isn't AWS    |
| `secrets.values.s3AccessKeyId/SecretAccessKey` | when `backend=s3` and not using IAM/instance profile |
| `app.oidc.issuer`                  | when OIDC is wanted                             |
| `secrets.values.oidcClientId/Secret` | when `app.oidc.issuer` is set                 |
| `ingress.host`                     | when `ingress.enabled=true`                     |
| `bucketCors.bucket/endpoint/allowedOrigin` | when `bucketCors.enabled=true`          |
| `blobCors.accountName/allowedOrigin` | when `blobCors.enabled=true` (azure backend) |

## How the moving parts fit together

**Migration Job** (Helm pre-install/pre-upgrade hook): runs `alembic
upgrade head` before the API pod starts so it never crashloops on a
behind schema. Disable with `migrationJob.enabled=false` if you run
alembic out of band.

**Bundled Postgres** (StatefulSet): single-replica, RWO PVC, no HA.
Good for dev + small prod. Disable + use `externalDatabase.url` for
managed or HA Postgres.

**Bucket CORS Job** (Helm post-install/post-upgrade hook): runs
`PutBucketCors` against the configured S3 endpoint via curl with
`--aws-sigv4`. Required for the SPA's "Show files" feature when the
storage origin differs from the app origin (every S3-compat self-host).
Idempotent.

**Blob CORS Job** (Helm post-install/post-upgrade hook): the Azure
counterpart, gated on `blobCors.enabled`. Runs `az storage cors add`
(clear-then-add for idempotency) to set a blob-service CORS rule on the
storage account. Azure CORS is account-level, not per-container, so one
rule covers every container. Same purpose as the S3 job: unblock the
SPA's "Show files" cross-origin fetch to `*.blob.core.windows.net`.

**NetworkPolicy** (off by default): one for the API pod (ingress from
your ingress controller's namespace) and one for the bundled Postgres
(ingress only from same-namespace pods, to support sibling backup
PreBackupPods that don't carry stable labels). Egress is intentionally
not restricted by the chart — locking it down requires
deployment-specific knowledge of where your DB / S3 / OIDC live.

**k8up resources** (off by default; needs the k8up operator
pre-installed): nightly `pg_dump → restic backup --stdin`, weekly
prune, monthly repo check. Schedules are configurable. Repo + REST
credentials live in operator-managed Secrets you reference by name.

**PrometheusRule / ConfigMap** (off by default): four alert rules
specific to conda-server's domain metrics — reindex failures,
mirror upstream error rate, scrape-target-missing, reindex-stuck.
Pick `metricsAlerts.kind: PrometheusRule` if you have the operator,
`ConfigMap` for plain Prometheus that globs a rules directory.

## Secret management

The chart ships a path of least resistance — set
`secrets.values.*` and the chart creates a Secret for you. **For
production prefer `secrets.existingSecret`** + your favourite secret
operator (External Secrets, Sealed Secrets, Vault Agent Injector,
SOPS) so the secret material doesn't end up rendered into Helm
release history.

The expected key names are configurable via `secretKeys.*` so an
existing Vault layout can map to the chart without renames.

## Things the chart deliberately leaves out

- **OIDC provider config** — manual, IdP-specific (see
  [`docs/deploying.md`](../../../docs/deploying.md) for the Authentik
  walkthrough).
- **Bundled Garage / MinIO / Ceph** — single-node Garage works but the
  cluster bootstrap (layout assign + key import) is finicky enough
  that it deserves its own chart. Use the `bucketCors` Job to attach a
  CORS rule to a pre-provisioned bucket.
- **Vault `ExternalSecret` CRDs** — too tied to your Vault path
  layout. Manage these in your own gitops repo and point at them via
  `secrets.existingSecret`.
- **ArgoCD `Application` object** — deployment-tool-specific.

## Validating local changes

```bash
helm lint deploy/helm/conda-server
helm template foo deploy/helm/conda-server -f deploy/helm/conda-server/ci/test-values.yaml | kubectl apply --dry-run=client -f -
helm template foo deploy/helm/conda-server -f deploy/helm/conda-server/ci/prod-values.yaml | kubectl apply --dry-run=client -f -
```

The prod-values render references the `monitoring.coreos.com/v1`
PrometheusRule CRD; expect a "no matches for kind" error when applying
against a cluster without prometheus-operator. That's not a chart bug.

## Release process

```bash
# Bump Chart.yaml version + appVersion if shipping a new app.
helm package deploy/helm/conda-server -d /tmp
# Push the .tgz to whatever chart registry you use (OCI, ChartMuseum,
# a static HTTP host with helm-repo-index, GitHub Pages, …).
```

There's no automated release publishing yet — the gitops repo
references the chart by path for now.
