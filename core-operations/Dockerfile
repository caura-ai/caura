# Dockerfile for core-operations
# Single-purpose service that hosts cron/scheduled background jobs.
# Minimal HTTP surface (only /healthz) — Cloud Run probes hit it.

# Optional Datadog serverless-init source (CAURA-0000 observability POC).
# SaaS builds pass --build-arg DD_APM_ENABLED=true to pull in Datadog's
# serverless-init binary; OSS / on-prem / local builds leave it false so the
# binary is never fetched (BuildKit prunes the unreferenced stage) and never
# lands in the image. Runtime activation is further gated by
# docker-entrypoint.sh on DD_API_KEY.
ARG DD_APM_ENABLED=false
FROM datadog/serverless-init:1@sha256:2f498f4b1165ac3551c34129b4f0c0c7bd99d82c55c4e95cf494b55dad8d05d0 AS dd-bin
FROM python:3.12-slim AS dd-apm-true
COPY --from=dd-bin /datadog-init /dd/datadog-init
FROM python:3.12-slim AS dd-apm-false
RUN mkdir -p /dd
FROM dd-apm-${DD_APM_ENABLED} AS dd-apm-src

FROM python:3.12-slim AS builder

WORKDIR /app

# Install Python deps FROZEN + hash-verified from the committed lock (CAURA-000):
# uv export keeps uv.lock's pins + per-package hashes, so the image is
# reproducible and every wheel is verified (require-hashes mode). Prevents the
# silent dep float that broke prod 2026-06-14.
COPY --from=ghcr.io/astral-sh/uv:0.9.18 /uv /usr/local/bin/uv
COPY core-operations/pyproject.toml core-operations/uv.lock ./core-operations/
RUN mkdir -p core-operations/src/core_operations && touch core-operations/src/core_operations/__init__.py
# DD_APM_ENABLED pulls in the ddtrace tracer via the ``datadog`` extra; only the
# SaaS build sets it, so OSS / on-prem images carry no Datadog Python code.
ARG DD_APM_ENABLED=false
RUN EXTRAS="--extra pubsub"; \
    if [ "$DD_APM_ENABLED" = "true" ]; then EXTRAS="$EXTRAS --extra datadog"; fi; \
    uv export --frozen --no-dev $EXTRAS --no-emit-project \
      --directory core-operations -o /tmp/reqs.txt \
    && uv pip install --system --no-cache -r /tmp/reqs.txt

COPY common/ /app/common/
COPY core-operations/src/ /app/core-operations/src/


FROM python:3.12-slim AS runtime

RUN useradd --create-home --shell /usr/sbin/nologin appuser

WORKDIR /app

# Copy the entire /usr/local from builder so future Python-base bumps
# (3.13, etc.) don't silently miss site-packages by hardcoded path.
COPY --from=builder /usr/local /usr/local
COPY --from=builder --chown=appuser:appuser /app /app

ENV PYTHONPATH="/app/core-operations/src:/app"
ENV PYTHONUNBUFFERED=1

# Datadog APM (opt-in; see top-of-file dd-apm stage + docker-entrypoint.sh).
# /app/dd is empty unless built with DD_APM_ENABLED=true.
COPY --from=dd-apm-src --chown=appuser:appuser /dd /app/dd
COPY --chown=appuser:appuser core-operations/docker-entrypoint.sh /app/docker-entrypoint.sh

USER appuser

EXPOSE 8080

# ENTRYPOINT is the opt-in Datadog shim; it exec's the CMD (plain, or wrapped
# under serverless-init + ddtrace-run when DD_API_KEY is set).
ENTRYPOINT ["/app/docker-entrypoint.sh"]
# `sh -c "exec …"` so PID 1 ends up as uvicorn (not dash). Without
# `exec` the shell stays as PID 1 and Debian's dash does NOT forward
# SIGTERM to its child — Cloud Run's grace period would then expire
# without scheduler.stop() running, and the container would be
# force-killed. The shell wrapper is still needed to expand $PORT
# (Cloud Run injects it) before the exec.
CMD ["sh", "-c", "exec uvicorn core_operations.app:app --host 0.0.0.0 --port ${PORT:-8080}"]
