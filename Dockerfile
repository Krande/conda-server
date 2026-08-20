# syntax=docker/dockerfile:1.7

# ---- Stage 1: frontend build ----
FROM node:22-alpine AS frontend
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json* ./
RUN --mount=type=cache,target=/root/.npm npm ci
# Bust the frontend build cache per commit. The CI runs docker in a persistent
# DinD whose BuildKit has been observed to reuse this stage's COPY/build layers
# across commits even when frontend/ changed — shipping a stale dist/ under a
# fresh image tag. Tying the layer to GIT_SHA forces `npm run build` to re-run
# for every commit; `npm ci` above stays cached (deps rarely change).
ARG GIT_SHA=dev
COPY frontend ./
RUN npm run build


# ---- Stage 2: Python environment build ----
FROM ghcr.io/prefix-dev/pixi:0.68.1 AS build

WORKDIR /app
COPY pixi.toml pixi.lock* pyproject.toml README.md ./
COPY src ./src

RUN pixi install --locked --environment default \
    && pixi run -e default python -c "import conda_server; print(conda_server.__version__)"

RUN pixi shell-hook -e default -s bash > /shell-hook.sh \
    && echo "exec \"\$@\"" >> /shell-hook.sh


# ---- Stage 3: runtime ----
# debian:bookworm-slim ships bash + shadow utils. ca-certificates and tini
# come from the pixi env, which avoids any apt reach-out — CI runners often
# restrict egress to HTTPS only and HTTP apt mirrors will hang.
FROM debian:bookworm-slim AS runtime

# Build-time provenance — surfaced by /api/about so the UI can show the
# exact commit the running pod was built from. CI passes the full SHA;
# manual local builds default to "unknown".
ARG GIT_SHA=unknown
ARG BUILD_DATE=unknown

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CONDA_SERVER_LOGGING__FORMAT=json \
    CONDA_SERVER_GIT_SHA=$GIT_SHA \
    CONDA_SERVER_BUILD_DATE=$BUILD_DATE

WORKDIR /app
COPY --from=build /app/.pixi /app/.pixi
COPY --from=build /app/src /app/src
COPY --from=build /app/pyproject.toml /app/README.md /app/
COPY --from=build /shell-hook.sh /shell-hook.sh
COPY --from=frontend /fe/dist /app/frontend/dist
COPY alembic.ini /app/alembic.ini

# Stable UID/GID so k8s securityContext can pin runAsUser/runAsGroup.
RUN groupadd --gid 1001 conda \
    && useradd --uid 1001 --gid 1001 --home /app --shell /usr/sbin/nologin conda \
    && chown -R conda:conda /app
USER 1001:1001

EXPOSE 8000
# No tini — uvicorn handles SIGTERM cleanly, and kubelet provides PID 1
# zombie reaping via shareProcessNamespace at the pod level if ever needed.
ENTRYPOINT ["bash", "/shell-hook.sh"]
CMD ["uvicorn", "conda_server.app:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips=*"]
