# Dockerfile for Core Storage API service
# PostgreSQL CRUD service for MemClaw core tables

FROM python:3.12-slim AS builder

WORKDIR /app

# System deps for asyncpg
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first (layer cache)
COPY core-storage-api/pyproject.toml core-storage-api/README.md* ./core-storage-api/
# Dummy src so setuptools find_packages doesn't fail
RUN mkdir -p core-storage-api/src/core_storage_api && touch core-storage-api/src/core_storage_api/__init__.py
RUN pip install --no-cache-dir ./core-storage-api

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
