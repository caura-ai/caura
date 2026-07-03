# Dockerfile for Core Worker service
# Async-embed event consumer — subscribes to memclaw.memory.embed-requested
# and PATCHes the resulting embedding back to core-storage-api.

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
COPY core-worker/pyproject.toml core-worker/uv.lock core-worker/README.md* ./core-worker/
# Dummy src so the project metadata resolves during export.
RUN mkdir -p core-worker/src/core_worker && touch core-worker/src/core_worker/__init__.py
# DD_APM_ENABLED pulls in the ddtrace tracer via the ``datadog`` extra; only the
# SaaS build sets it, so OSS / on-prem images carry no Datadog Python code.
ARG DD_APM_ENABLED=false
RUN EXTRAS="--extra pubsub --extra vertex"; \
    if [ "$DD_APM_ENABLED" = "true" ]; then EXTRAS="$EXTRAS --extra datadog"; fi; \
    uv export --frozen --no-dev $EXTRAS --no-emit-project \
      --directory core-worker -o /tmp/reqs.txt \
    && uv pip install --system --no-cache -r /tmp/reqs.txt

# Copy actual source
COPY common/ /app/common/
COPY core-worker/src/ /app/core-worker/src/


# ── Production image ──
FROM python:3.12-slim AS runtime

RUN useradd --create-home --shell /bin/bash appuser

WORKDIR /app

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder --chown=appuser:appuser /app /app

# common/ is a plain package — make it importable via PYTHONPATH
ENV PYTHONPATH="/app/core-worker/src:/app"
ENV PYTHONUNBUFFERED=1

# Datadog APM (opt-in; see top-of-file dd-apm stage + docker-entrypoint.sh).
# /app/dd is empty unless built with DD_APM_ENABLED=true.
COPY --from=dd-apm-src --chown=appuser:appuser /dd /app/dd
COPY --chown=appuser:appuser core-worker/docker-entrypoint.sh /app/docker-entrypoint.sh

USER appuser

EXPOSE 8080

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uvicorn", "core_worker.app:app", "--host", "0.0.0.0", "--port", "8080"]
