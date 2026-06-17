# Dockerfile for Core API service
# The main MemClaw API — auth, agents, memory, MCP, etc.

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
RUN uv export --frozen --no-dev --extra pubsub --no-emit-project \
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

USER appuser

EXPOSE 8000

CMD ["uvicorn", "core_api.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--timeout-keep-alive", "65"]
