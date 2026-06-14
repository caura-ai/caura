# Dockerfile for core-operations
# Single-purpose service that hosts cron/scheduled background jobs.
# Minimal HTTP surface (only /healthz) — Cloud Run probes hit it.

FROM python:3.12-slim AS builder

WORKDIR /app

# Install Python deps FROZEN + hash-verified from the committed lock (CAURA-000):
# uv export keeps uv.lock's pins + per-package hashes, so the image is
# reproducible and every wheel is verified (require-hashes mode). Prevents the
# silent dep float that broke prod 2026-06-14.
COPY --from=ghcr.io/astral-sh/uv:0.9.18 /uv /usr/local/bin/uv
COPY core-operations/pyproject.toml core-operations/uv.lock ./core-operations/
RUN mkdir -p core-operations/src/core_operations && touch core-operations/src/core_operations/__init__.py
RUN uv export --frozen --no-dev --extra pubsub --no-emit-project \
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

USER appuser

EXPOSE 8080

# `sh -c "exec …"` so PID 1 ends up as uvicorn (not dash). Without
# `exec` the shell stays as PID 1 and Debian's dash does NOT forward
# SIGTERM to its child — Cloud Run's grace period would then expire
# without scheduler.stop() running, and the container would be
# force-killed. The shell wrapper is still needed to expand $PORT
# (Cloud Run injects it) before the exec.
CMD ["sh", "-c", "exec uvicorn core_operations.app:app --host 0.0.0.0 --port ${PORT:-8080}"]
