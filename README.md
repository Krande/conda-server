# conda-server

A modern, open-source conda package server built on the [rattler](https://github.com/conda/rattler) ecosystem.

**Status:** early development — API and data model are not yet stable.

## What it does

- Serves `repodata.json` and `.conda` packages over HTTP for any `conda` / `mamba` / `pixi` client.
- Stores package bytes in pluggable object storage (S3, Azure Blob, GCS, or local filesystem).
- Indexes packages using [`py-rattler`](https://github.com/conda/rattler) and [`rattler-index`](https://github.com/conda/rattler) — no `conda-build` dependency.
- OIDC login (GitHub, Google, Azure AD, generic) and bearer tokens for CLI/CI.
- Ships as a single Docker image; scales horizontally behind any standard reverse proxy.

See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for design decisions.

## Quick start (local dev)

Requires [pixi](https://pixi.sh).

```bash
pixi install
pixi run migrate            # create SQLite DB
pixi run serve              # backend on http://localhost:8000

# In another terminal, for the SPA:
pixi run -e dev frontend-install
pixi run -e dev frontend-dev   # http://localhost:5173 (proxies /api to the backend)
```

The default configuration uses SQLite and a local `./data` directory for package storage. Edit `conda-server.toml` (or set `CONDA_SERVER_*` env vars) to point at Postgres / S3 / Azure.

### Frontend

The web UI is a React 19 + Vite 6 + Tailwind 4 SPA using TanStack Query for data fetching. In production it's served from the same origin as the API — the backend auto-mounts `frontend/dist` when present. Build it with:

```bash
pixi run -e dev frontend-build
```

## Configuration

Configuration is layered: defaults → `conda-server.toml` → environment variables. See `conda-server.example.toml` for all available keys.

Common env vars:

```
CONDA_SERVER_HOST=0.0.0.0
CONDA_SERVER_PORT=8000
CONDA_SERVER_BASE_URL=https://conda.example.com

CONDA_SERVER_DATABASE__URL=postgresql+asyncpg://user:pass@host/db

CONDA_SERVER_STORAGE__BACKEND=s3
CONDA_SERVER_STORAGE__URL=s3://my-conda-bucket/channels
CONDA_SERVER_STORAGE__REGION=eu-west-1

CONDA_SERVER_AUTH__SESSION_SECRET=<openssl rand -hex 32>
CONDA_SERVER_AUTH__INITIAL_ADMINS=["you@example.com"]
CONDA_SERVER_AUTH__OIDC__ISSUER=...
CONDA_SERVER_AUTH__OIDC__CLIENT_ID=...
CONDA_SERVER_AUTH__OIDC__CLIENT_SECRET=...
```

## OIDC / SSO

Any OIDC-compliant provider works (Authentik, Keycloak, Authelia, Azure AD, Google, GitHub-as-OIDC). The server discovers endpoints via `{issuer}/.well-known/openid-configuration`.

**Login flow:** `GET /api/auth/login` → redirect to IdP → `GET /api/auth/callback` → session cookie set → redirect to `/` (or `?redirect=/admin`). `GET /api/auth/me` returns the current user. `POST /api/auth/logout` clears the session.

**Admin bootstrap:** on first login, emails listed in `auth.initial_admins` are promoted to the `admin` role. Case-insensitive.

### Authentik

1. **Create an OAuth2 / OpenID Provider** in Authentik. Note the generated *Client ID* and *Client Secret*; set *Client type* = **Confidential**. Signing key: any. Subject mode: "Based on the User's hashed ID" or whichever you prefer — conda-server stores whatever Authentik emits in the `sub` claim.
2. **Create an Application** linked to that provider.
3. **Register the redirect URI** exactly as: `https://<your-conda-server>/api/auth/callback`
4. **Scopes:** `openid`, `email`, `profile` (already the default).
5. **Configure conda-server:**

   ```toml
   [auth]
   session_secret = "..."   # openssl rand -hex 32
   initial_admins = ["you@your-domain.com"]

   [auth.oidc]
   issuer = "https://authentik.example.com/application/o/<app-slug>/"
   client_id = "..."
   client_secret = "..."
   scopes = ["openid", "email", "profile"]
   ```

   The trailing slash on the Authentik `issuer` matters — it's part of the URL Authentik publishes in its discovery document.

### Behind a reverse proxy

When TLS is terminated upstream (k8s ingress, nginx, Traefik), uvicorn must be started with `--proxy-headers --forwarded-allow-ips='*'` so that `request.url_for()` returns `https://…` — otherwise the OIDC `redirect_uri` won't match what's registered with the IdP. The shipped Dockerfile does this.

## Deployment

Published images are pushed to the GitHub Container Registry on each
release tag:

```bash
docker run -p 8000:8000 ghcr.io/krande/conda-server:latest
```

See [`docs/deploying.md`](./docs/deploying.md) for the full production
walkthrough, including the non-obvious bits — path-style vs
virtual-host-style S3, CORS on object storage, `SSL_CERT_FILE` for
rustls on a Debian-slim runtime, NetworkPolicy pitfalls around
kube-proxy DNAT, and a restore drill for the backup configuration.

A multi-stage `Dockerfile` produces a slim runtime image, and a packaged
Helm chart for Kubernetes ships in
[`deploy/helm/conda-server/`](./deploy/helm/conda-server/).

## License

[BSD 3-Clause](./LICENSE) — matches the conda ecosystem.
