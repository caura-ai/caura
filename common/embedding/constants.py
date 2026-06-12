"""Embedding-side default constants — env-overridable.

Kept env-driven so this module has no dependency on any service's
``config`` module: both core-api (tenant-aware) and core-worker
(platform-only) read the same env vars, populated by their respective
``BaseServiceSettings`` classes upstream.

NOTE: every constant below is evaluated ONCE at module-import time.
Tests that need a different value at runtime must patch the module-
level binding directly — ``monkeypatch.setenv`` after import is too
late. Examples:

    monkeypatch.setattr("common.embedding._service.EMBEDDING_RETRY_ATTEMPTS", 1)
    monkeypatch.setattr("common.embedding.providers.openai.OPENAI_REQUEST_TIMEOUT_SECONDS", 5.0)
"""

from __future__ import annotations

import os

# Default model identifiers per provider. Override via env (e.g. swap
# ``OPENAI_EMBEDDING_MODEL=text-embedding-3-large`` for a higher-dim
# variant; pair with a ``VECTOR_DIM`` change at the schema level).
#
# Use ``or`` rather than the ``get`` default so an *empty* value falls back
# too: docker-compose maps ``OPENAI_EMBEDDING_MODEL: "${OPENAI_EMBEDDING_MODEL:-}"``,
# so an unset var in ``.env`` reaches the container as "" (present-but-empty),
# which ``os.environ.get(key, default)`` would NOT replace — leaving model=""
# and every embed call failing with OpenAI 400 "you must provide a model".
OPENAI_EMBEDDING_MODEL: str = (
    os.environ.get("OPENAI_EMBEDDING_MODEL") or "text-embedding-3-small"
)

# Per-call OpenAI timeout. Caps a single embed/embed_batch round trip
# (TLS handshake + request + retry-with-backoff inside the SDK). Same
# default + env shape as ``core_api.config.settings.openai_request_timeout_seconds``
# so a single tunable controls both the LLM and the embedding paths.
# Without this, a hung api.openai.com response would ride the SDK's
# default 600s timeout and silently eat the worker's whole ack budget.
OPENAI_REQUEST_TIMEOUT_SECONDS: float = float(
    os.environ.get("OPENAI_REQUEST_TIMEOUT_SECONDS", "25.0")
)

# Retry budget for the high-level ``get_embedding`` call. Two attempts
# is enough to ride out a single slow / 429 round-trip without
# meaningfully extending the hot-path tail.
EMBEDDING_RETRY_ATTEMPTS: int = int(os.environ.get("EMBEDDING_RETRY_ATTEMPTS", "2"))
EMBEDDING_RETRY_DELAY_S: float = float(os.environ.get("EMBEDDING_RETRY_DELAY_S", "1.0"))


# httpx pool sizing for the embedding-side OpenAI client (CAURA-627).
# Same env var names as ``common/llm/constants.py`` so a single env
# tunable controls both the LLM and the embedding pools. Without an
# explicit limits arg the SDK rides httpx's default (100 max / 20
# keepalive) which saturates under bulk-write storm fan-out (16
# concurrent writes × 10 enrichment calls = 160 concurrent LLM
# requests per process, well over the keepalive budget). See
# ``common/llm/constants.py`` for full rationale.
from common.env_utils import clamp_keepalive, read_int_env  # noqa: E402

OPENAI_HTTPX_MAX_CONNECTIONS: int = read_int_env("OPENAI_HTTPX_MAX_CONNECTIONS", 200)
OPENAI_HTTPX_MAX_KEEPALIVE_CONNECTIONS: int = clamp_keepalive(
    OPENAI_HTTPX_MAX_CONNECTIONS,
    read_int_env("OPENAI_HTTPX_MAX_KEEPALIVE_CONNECTIONS", 50),
)
