# Dockerfile for Core Storage API service
# PostgreSQL CRUD service for MemClaw core tables

FROM python:3.12-slim AS builder

WORKDIR /app

# System deps for asyncpg
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps FROZEN + hash-verified from the committed lock (CAURA-000):
# uv export keeps uv.lock's pins + per-package hashes, so the image is
# reproducible and every wheel is verified (require-hashes mode). Prevents the
# silent dep float that broke prod 2026-06-14.
COPY --from=ghcr.io/astral-sh/uv:0.9.18 /uv /usr/local/bin/uv
COPY core-storage-api/pyproject.toml core-storage-api/uv.lock core-storage-api/README.md* ./core-storage-api/
# Dummy src so the project metadata resolves during export.
RUN mkdir -p core-storage-api/src/core_storage_api && touch core-storage-api/src/core_storage_api/__init__.py
RUN uv export --frozen --no-dev --no-emit-project \
      --directory core-storage-api -o /tmp/reqs.txt \
    && uv pip install --system --no-cache -r /tmp/reqs.txt

# Copy actual source
COPY common/ /app/common/
COPY core-storage-api/src/ /app/core-storage-api/src/


# ── Production image ──
FROM python:3.12-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash appuser

WORKDIR /app

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder --chown=appuser:appuser /app /app

# common/ is a plain package — make it importable via PYTHONPATH
ENV PYTHONPATH="/app/core-storage-api/src:/app"
ENV PYTHONUNBUFFERED=1
ENV ENVIRONMENT=production

USER appuser

EXPOSE 8002

CMD ["uvicorn", "core_storage_api.app:app", "--host", "0.0.0.0", "--port", "8002", "--workers", "2", "--timeout-keep-alive", "65"]
