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

# Per-phase connect timeout, mirroring ``OPENAI_HTTPX_CONNECT_TIMEOUT_SECONDS``
# on the LLM side. Passing a bare float to ``AsyncOpenAI(timeout=...)`` looks
# like it sets the whole budget but leaves httpx's DEFAULT 5 s connect phase in
# place. On Cloud Run with a VPC connector in ``all-traffic`` egress mode every
# outbound call intermittently exceeds 5 s, which surfaces as
# ``httpcore.ConnectTimeout`` well inside the nominal request budget. The LLM
# client took this fix; the embedding client kept the bare float, so the same
# trickle continued here — visible as the staging
# "pubsub handler raised; nacking for redelivery" loop, where the worker's
# embed call is the only handler that can propagate. Only connect/pool get the
# headroom; the read phase stays governed by ``OPENAI_REQUEST_TIMEOUT_SECONDS``.
EMBEDDING_HTTPX_CONNECT_TIMEOUT_SECONDS: float = read_float_env(
    "EMBEDDING_HTTPX_CONNECT_TIMEOUT_SECONDS", 15.0
)
# None ⇒ the pool phase tracks the request budget, preserving the bare-float
# behaviour this replaces for every existing configuration. Set the env var
# only to decouple them (e.g. fail fast under pool pressure).
EMBEDDING_HTTPX_POOL_TIMEOUT_SECONDS: float | None = read_float_env(
    "EMBEDDING_HTTPX_POOL_TIMEOUT_SECONDS", None
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

# How far under a caller's own deadline this module aims to fail.
#
# Callers enforce their budgets by CANCELLING us (``asyncio.timeout`` /
# ``wait_for``), and a cancellation carries no attribution — it says the
# deadline passed, not which layer ate it. The module already applies this
# principle to the concurrency gate (see EMBEDDING_GATE_TIMEOUT_SECONDS:
# "deliberately BELOW the callers' deadlines ... so the gate could attribute
# the failure"), but the provider call itself was left unbounded, so a slow
# backend surfaced as the caller's anonymous timeout.
#
# One margin, subtracted from whatever budget the caller passes, so the
# ordering holds however an operator retunes the budgets rather than being
# re-derived per call site. 1 s because the budgets it sits under are 8 s and
# 30 s, and because the thing being raced is a network round trip whose
# measured p95 against a loaded sidecar is ~0.4 s — a margin of that order is
# generous without meaningfully shortening the useful budget.
EMBEDDING_BUDGET_MARGIN_S: float = read_float_env("EMBEDDING_BUDGET_MARGIN_S", 1.0)

# Cap the texts sent in ONE provider request; larger inputs are split.
#
# This is admission control on the SERVER's side of the wire, mirrored on
# ours. A self-hosted TEI sidecar defaults ``--max-client-batch-size`` to
# 32 and answers anything larger with
# ``413 batch size N > maximum allowed batch size 32`` — while our bulk
# write path sends up to ``BULK_MAX_ITEMS`` (100) texts in one call and
# the re-embed path sends ``EMBEDDING_REEMBED_BATCH_SIZE`` (50). Neither
# chunked, so both were rejected outright.
#
# That ran unnoticed in production for 30+ days: the failure cascades into
# a per-item fallback that produces CORRECT embeddings, so the only
# symptoms were ~50x the requests and the sidecar's own 413 log. Raising
# the server's cap fixes one deployment; capping here fixes every
# deployment, including the docker-compose local-embedder stack, which
# starts TEI without the flag.
#
# 32 to match TEI's own default — the safe assumption about a backend
# whose cap we cannot see. This applies to SELF-HOSTED, OpenAI-compatible
# backends, i.e. those reached via ``base_url``; see
# ``EMBEDDING_HOSTED_MAX_BATCH`` for the hosted default and
# ``OpenAIEmbeddingProvider`` for the dispatch.
#
# ``common/ranking`` reached the identical conclusion first, for the same
# sidecar and the same 413 — see ``RANK_REMOTE_MAX_BATCH``.
EMBEDDING_REMOTE_MAX_BATCH: int = read_int_env("EMBEDDING_REMOTE_MAX_BATCH", 32)

# The same cap for HOSTED OpenAI — no ``base_url``, so we know the backend
# and its documented limit: 2048 inputs per embeddings request.
#
# Split from the self-hosted value rather than sharing one number, because
# 32 is a guess about an opaque sidecar and 2048 is a documented fact
# about a known API. Collapsing them would silently turn one hosted bulk
# write of BULK_MAX_ITEMS (100) into 4 sequential round trips for no
# reason — chunking should cost nothing where the backend accepts the
# whole batch. Still env-overridable so a hosted deployment that wants
# smaller requests (rate-limit shaping, latency smoothing) has a
# config-only lever.
EMBEDDING_HOSTED_MAX_BATCH: int = read_int_env("EMBEDDING_HOSTED_MAX_BATCH", 2048)
