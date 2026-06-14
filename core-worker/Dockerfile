# Dockerfile for Core Worker service
# Async-embed event consumer — subscribes to memclaw.memory.embed-requested
# and PATCHes the resulting embedding back to core-storage-api.

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
RUN uv export --frozen --no-dev --extra pubsub --extra vertex --no-emit-project \
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

USER appuser

EXPOSE 8080

CMD ["uvicorn", "core_worker.app:app", "--host", "0.0.0.0", "--port", "8080"]
