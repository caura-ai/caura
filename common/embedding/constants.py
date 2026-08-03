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
# concurrent writes x 10 enrichment calls = 160 concurrent LLM
# requests per process, well over the keepalive budget). See
# ``common/llm/constants.py`` for full rationale.
from common.env_utils import (  # noqa: E402 — intentional late import, see module docstring
    clamp_keepalive,
    read_float_env,
    read_int_env,
)

OPENAI_HTTPX_MAX_CONNECTIONS: int = read_int_env("OPENAI_HTTPX_MAX_CONNECTIONS", 200)
OPENAI_HTTPX_MAX_KEEPALIVE_CONNECTIONS: int = clamp_keepalive(
    OPENAI_HTTPX_MAX_CONNECTIONS,
    read_int_env("OPENAI_HTTPX_MAX_KEEPALIVE_CONNECTIONS", 50),
)

# Dedicated embedding-pool knobs, defaulting to the shared
# ``OPENAI_HTTPX_*`` values above for backward compatibility.
#
# Why these exist: the embedding backend is frequently NOT OpenAI. Prod
# points ``OPENAI_EMBEDDING_BASE_URL`` at a self-hosted TEI service whose
# total capacity is a couple of GPU instances, while the LLM path talks
# to a hyperscaler API that happily absorbs a 200-connection pool. Before
# this split both paths read the same env var, so sizing the embedding
# pool down to match a small self-hosted backend would also throttle
# every LLM call — making the one knob you need during an embedding
# incident unusable.
EMBEDDING_HTTPX_MAX_CONNECTIONS: int = read_int_env(
    "EMBEDDING_HTTPX_MAX_CONNECTIONS", OPENAI_HTTPX_MAX_CONNECTIONS
)
# The inherited default is pre-clamped: sizing ONLY
# EMBEDDING_HTTPX_MAX_CONNECTIONS below the LLM keepalive would otherwise
# trip clamp_keepalive's warning naming a var the operator never set.
# An explicit over-large keepalive still warns, which is the point.
EMBEDDING_HTTPX_MAX_KEEPALIVE_CONNECTIONS: int = clamp_keepalive(
    EMBEDDING_HTTPX_MAX_CONNECTIONS,
    read_int_env(
        "EMBEDDING_HTTPX_MAX_KEEPALIVE_CONNECTIONS",
        min(OPENAI_HTTPX_MAX_KEEPALIVE_CONNECTIONS, EMBEDDING_HTTPX_MAX_CONNECTIONS),
    ),
    max_connections_var="EMBEDDING_HTTPX_MAX_CONNECTIONS",
    max_keepalive_var="EMBEDDING_HTTPX_MAX_KEEPALIVE_CONNECTIONS",
)


# ── Client-side concurrency cap (backpressure) ───────────────────────
#
# The embedding backend is finite and can be much smaller than the
# fleet calling it: prod's TEI service serves
# ``maxScale x containerConcurrency`` requests at once, while core-api
# scales to many instances that each hold a large httpx pool. Aggregate
# demand can therefore oversubscribe the backend by orders of magnitude.
#
# When that happens the backend itself stays healthy (it just queues) but
# every caller waits at the HTTP pool and dies on ``httpx.PoolTimeout``
# -> ``APITimeoutError`` — and the per-item fallback for a failed batch
# re-jams the same slots, so the failure sustains itself. That is exactly
# the 2026-07-27 prod incident: ~77% of embeds failing for ~1h45m with
# TEI reporting 3.5 ms inference throughout, leaving ~430 memories
# permanently unembedded.
#
# Capping in-flight provider calls per process makes surplus work wait on
# a cheap in-process semaphore instead of holding a connection until it
# times out.
#
# Be honest about the bound: this is PER PROCESS, so the fleet-wide total
# is this value x instance count. At the default 16 x minScale 10 that is
# still ~8x TEI's capacity, and far more at maxScale — so this reduces
# thrash AMPLITUDE, it does not restore a capacity invariant. Raising TEI
# maxScale/containerConcurrency is the actual capacity fix; set this
# explicitly per deployment with the arithmetic written down.
EMBEDDING_MAX_CONCURRENCY: int = read_int_env("EMBEDDING_MAX_CONCURRENCY", 16)

# Bound the wait for a slot. Queueing is the point, but an unbounded wait
# would let waiters pile up and stall the write path well past its own
# budget. On timeout the caller degrades exactly as a provider failure
# does (persist ``embedding=NULL``, leave it to re-embed) instead of
# hanging — same outcome as before this cap existed, minus the
# connection thrash that produced it.
#
# Deliberately BELOW the callers' own deadlines: the bulk paths already
# arm 30 s budgets (``BULK_EMBEDDING_TIMEOUT_SECONDS`` and the
# ``asyncio.wait_for`` around ``get_embeddings_batch``). At 30 s here the
# outer timer would always fire first, so the gate could never attribute
# the failure to backpressure — it would surface as the caller's generic
# timeout instead. 5 s leaves the queue useful while keeping the
# attribution (and the degradation) on the gate.
EMBEDDING_GATE_TIMEOUT_SECONDS: float = read_float_env(
    "EMBEDDING_GATE_TIMEOUT_SECONDS", 5.0
)
