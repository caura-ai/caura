# Dockerfile for Core API service
# The main MemClaw API — auth, agents, memory, MCP, etc.

# Optional Datadog serverless-init source (CAURA-0000 observability POC).
# The Caura SaaS deploy builds with --build-arg DD_APM_ENABLED=true to pull in
# Datadog's serverless-init binary; OSS / on-prem / local builds leave it
# ``false`` so the binary is never fetched (BuildKit prunes the unreferenced
# datadog/serverless-init stage) and never lands in the image. Runtime
# activation is further gated by docker-entrypoint.sh on DD_API_KEY, so even a
# SaaS image stays a no-op until a key is supplied.
ARG DD_APM_ENABLED=false
# Pinned by digest (tag :1 floats) so the embedded binary is reproducible.
# datadog/serverless-init:1 as of 2026-06-30; bump deliberately.
FROM datadog/serverless-init:1@sha256:2f498f4b1165ac3551c34129b4f0c0c7bd99d82c55c4e95cf494b55dad8d05d0 AS dd-bin
FROM python:3.12-slim AS dd-apm-true
COPY --from=dd-bin /datadog-init /dd/datadog-init
FROM python:3.12-slim AS dd-apm-false
RUN mkdir -p /dd
FROM dd-apm-${DD_APM_ENABLED} AS dd-apm-src

FROM python:3.12-slim AS builder

WORKDIR /app

# System deps for asyncpg / hiredis
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps FROZEN from the committed lock (CAURA-000 pilot). Export
# the locked versions and install them, so the image can't silently float to a
# new release between builds — a lock-less ``pip install`` floated to FastAPI
# 0.137.0 and crashed prod 2026-06-14. Dep bumps are now deliberate ``uv lock``
# changes, visible in review. [pubsub] extra adds google-cloud-pubsub (needed
# when EVENT_BUS_BACKEND=pubsub; lazily imported otherwise). The export keeps the
# per-package hashes from uv.lock, so `uv pip install` enters require-hashes mode
# and verifies every downloaded wheel against the lock (supply-chain integrity).
COPY --from=ghcr.io/astral-sh/uv:0.9.18 /uv /usr/local/bin/uv
COPY core-api/pyproject.toml core-api/uv.lock core-api/README.md* ./core-api/
# Dummy src so the project metadata resolves during export.
RUN mkdir -p core-api/src/core_api && touch core-api/src/core_api/__init__.py
# Document-ingest (kreuzberg, ~64MB) is an opt-in extra — off by default to
# keep the image small and cold-start fast. Build with --build-arg WITH_INGEST=1
# for a deployment that serves the /ingest/* endpoints.
ARG WITH_INGEST=0
# DD_APM_ENABLED pulls in the ddtrace tracer via the ``datadog`` extra; only the
# SaaS build sets it, so OSS / on-prem images carry no Datadog Python code.
ARG DD_APM_ENABLED=false
RUN EXTRAS="--extra pubsub"; \
    if [ "$WITH_INGEST" = "1" ]; then EXTRAS="$EXTRAS --extra ingest"; fi; \
    if [ "$DD_APM_ENABLED" = "true" ]; then EXTRAS="$EXTRAS --extra datadog"; fi; \
    uv export --frozen --no-dev $EXTRAS --no-emit-project \
      --directory core-api -o /tmp/reqs.txt \
    && uv pip install --system --no-cache -r /tmp/reqs.txt

# Stamp the service version into a file the app reads at runtime. Source of
# truth is pyproject.toml; an optional MEMCLAW_VERSION build-arg (e.g. the
# release tag) overrides it. Deterministic and independent of installed-
# package metadata — deps install via ``--no-emit-project`` so core-api is
# never installed as a package, leaving importlib.metadata with no version
# to find (the endpoint silently served "dev"). Read by
# core_api.constants._resolve_version().
ARG MEMCLAW_VERSION=
RUN VER="${MEMCLAW_VERSION:-$(python -c "import tomllib; print(tomllib.load(open('core-api/pyproject.toml','rb'))['project']['version'])")}" \
    && printf '%s' "$VER" > /app/VERSION \
    && echo "Stamped MemClaw version: $VER"

# Copy actual source
COPY common/ /app/common/
COPY core-api/src/ /app/core-api/src/
COPY plugin/ /app/plugin/
COPY static/ /app/static/


# ── Production image ──
FROM python:3.12-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash appuser

WORKDIR /app

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder --chown=appuser:appuser /app /app

# common/ is a plain package — make it importable via PYTHONPATH
ENV PYTHONPATH="/app/core-api/src:/app"
ENV PYTHONUNBUFFERED=1
ENV ENVIRONMENT=production

# Datadog APM (CAURA-0000 observability POC) — single-container Cloud Run
# pattern. serverless-init starts an in-process trace/log agent on localhost and
# execs the app under ``ddtrace-run`` (no sidecar; the datadog-ci sidecar path is
# incompatible with our VPC-connector services, and this survives pipeline
# redeploys). The wrapper is OPT-IN: docker-entrypoint.sh only invokes it when
# DD_API_KEY is set (the SaaS deploy), so OSS / on-prem / local runs get plain
# uvicorn with no Datadog. The binary is only present in DD_APM_ENABLED=true
# builds (see the dd-apm-src stage at the top of this file); /app/dd is empty
# otherwise.
COPY --from=dd-apm-src --chown=appuser:appuser /dd /app/dd
# docker-entrypoint.sh is committed with the executable bit set, so COPY
# preserves it — no chmod layer needed.
COPY --chown=appuser:appuser core-api/docker-entrypoint.sh /app/docker-entrypoint.sh

USER appuser

EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uvicorn", "core_api.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--timeout-keep-alive", "65"]
