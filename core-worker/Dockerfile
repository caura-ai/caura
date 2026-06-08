# Dockerfile for Core Worker service
# Async-embed event consumer — subscribes to memclaw.memory.embed-requested
# and PATCHes the resulting embedding back to core-storage-api.

FROM python:3.12-slim AS builder

WORKDIR /app

# Install Python deps first (layer cache)
COPY core-worker/pyproject.toml core-worker/README.md* ./core-worker/
# Dummy src so setuptools find_packages doesn't fail
RUN mkdir -p core-worker/src/core_worker && touch core-worker/src/core_worker/__init__.py
RUN pip install --no-cache-dir './core-worker[pubsub,vertex]'

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
