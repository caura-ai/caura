# Dockerfile for Core API service
# The main MemClaw API — auth, agents, memory, MCP, etc.

FROM python:3.12-slim AS builder

WORKDIR /app

# System deps for asyncpg / hiredis
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first (layer cache)
COPY core-api/pyproject.toml core-api/README.md* ./core-api/
# Dummy src so setuptools finds_packages doesn't fail
RUN mkdir -p core-api/src/core_api && touch core-api/src/core_api/__init__.py
# [pubsub] extra installs google-cloud-pubsub. Required when
# EVENT_BUS_BACKEND=pubsub (SaaS); harmless in OSS standalone where
# the inprocess bus is the default — the SDK is imported lazily by
# PubSubEventBus only when that backend is selected. Matches the
# install pattern used by core-worker/Dockerfile.
RUN pip install --no-cache-dir './core-api[pubsub]'

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

USER appuser

EXPOSE 8000

CMD ["uvicorn", "core_api.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--timeout-keep-alive", "65"]
