# Developing and running conda-server

Everything in here is for people working on conda-server or running an
instance of it. Using one — finding, installing and publishing packages — is
covered in the [README](./README.md).

See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for the design decisions behind it.

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

Signing in needs an OIDC provider (see [OIDC / SSO](#oidc--sso)); there is no
dev login. Anonymous browsing of public channels works without one.

### Frontend

The web UI is a React 19 + Vite 6 + Tailwind 4 SPA using TanStack Query for data fetching. In production it's served from the same origin as the API — the backend auto-mounts `frontend/dist` when present. Build it with:

```bash
pixi run -e dev frontend-build
```

## Tests and checks

| Command | What it runs |
|---|---|
| `pixi run -e dev test` | the Python test suite |
| `pixi run lint` | `ruff format --check` + `ruff check`, with the exact ruff version CI pins |
| `pixi run -e dev format` | `ruff format` over `src` and `tests` |
| `pixi run -e dev typecheck` | mypy |
| `pixi run -e dev frontend-lint` | ESLint over the SPA |
| `pixi run -e dev e2e` | Playwright smoke tests: builds the SPA, starts a backend on :8001, runs `frontend/e2e/` |

The e2e suite needs a browser once per machine: `pixi run -e dev e2e-install`.
It only covers anonymous pages, since signing in requires a live identity
provider.

ruff is the only formatter and linter; black and isort are deliberately not
used, since they disagree with `ruff format`.

## README screenshots

The figures in the README are generated, not hand-made:

```bash
pixi run ui-stories                     # every story → docs/screenshots/<name>.png
pixi run ui-stories --list
pixi run ui-stories --story package     # just one; repeatable
pixi run ui-stories --theme light       # → <name>-light.png (the README uses dark, amber)
pixi run ui-stories --keep              # leave the demo server running afterwards
```

[`scripts/ui_stories.py`](./scripts/ui_stories.py) boots a throwaway server of
its own — SQLite and local storage under `.pixi/ui-stories/`, wiped on every
run — so it needs neither a running dev stack nor an identity provider. It
creates the demo users through the same function an OIDC callback calls, hands
the browser a session cookie signed with the throwaway secret, and seeds three
channels (public, private, a mirror) through the HTTP API, with a handful of
`.conda` archives assembled at seed time. The browser is pointed at
`http://conda.example.com` and every request is rerouted to the local server,
so install commands in the figures read like a real deployment's.

The task builds the SPA first (in the `dev` environment) and downloads
Chromium into `.pixi/ms-playwright` on first use. Each story is one decorated
function in the script: add one there, then reference its PNG from the README.
Re-run the stories whenever a PR changes a screen the README shows.

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

## Operator CLI

The `conda-server` command talks to the database and object storage directly,
bypassing HTTP — for jobs that shouldn't wait on a request, and for rescuing a
channel nobody can manage from the web UI:

```bash
conda-server reindex <channel> [--verify]        # rebuild repodata from what's in storage
conda-server backfill-about <channel> [--limit N]  # read about.json from older archives
conda-server channel grant <channel> <email> [reader|writer|owner]
conda-server channel revoke <channel> <email>
conda-server channel members <channel>
```

`--help` on each explains what it touches.

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

## Releases

Releases are cut by merging a PR into `main`. The
[`tag-on-pr-merge`](./.github/workflows/tag-on-pr-merge.yaml) workflow hands
the decision to [deputy](https://github.com/Krande/deputy), which reads the
PR's `release-*` label, bumps the version in `pyproject.toml`, tags `vX.Y.Z`
and cuts a GitHub Release. The tag triggers
[`build-and-push`](./.github/workflows/build-and-push.yaml), which publishes
the image as `:X.Y.Z`, `:X.Y`, `:latest` and `:sha-<short>`. Repo-specific
release settings live in [`deputy.toml`](./deputy.toml).
