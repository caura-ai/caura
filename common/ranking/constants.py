"""Ranking-side default constants — env-overridable.

Kept env-driven so this module has no dependency on any service's
``config`` module, mirroring ``common/embedding/constants.py``. Ranking
runs only on the core-api search path (never the async write worker), so
there is no worker-tier variant to mirror.

NOTE: every constant below is evaluated ONCE at module-import time. Tests
that need a different value must patch the module-level binding directly
(``monkeypatch.setattr("common.ranking.constants.RANK_CANDIDATE_LIMIT", 5)``)
— ``monkeypatch.setenv`` after import is too late.
"""

from __future__ import annotations

import os

# Master on/off for the rerank step. When false (the default) the
# RerankResults step is skipped entirely — a zero-cost kill-switch
# (no candidates built, no service call) independent of RANK_PROVIDER,
# which selects *which* ranker runs when enabled. Keeps the component
# dark by default; flip to true (plus a non-noop RANK_PROVIDER) to turn
# reranking on. Per-tenant override: tenant_config.rank_enabled.
RANK_ENABLED: bool = os.environ.get("RANK_ENABLED", "").strip().lower() in (
    "true",
    "1",
    "yes",
    "on",
)

# Provider selection when reranking is enabled. ``noop`` = identity (keep
# first-stage order); ``local`` = in-process MiniLM cross-encoder; ``fake``
# = tests. Mirrors ``EMBEDDING_PROVIDER``.
RANK_PROVIDER: str = os.environ.get("RANK_PROVIDER") or "noop"

# Cross-encoder model for the in-process ``local`` provider. MiniLM-L6
# (22M) was measured ~= bge-reranker-base (278M) on our data (A50 quality
# spike) while being CPU-viable (~50-150ms at pool<=20); bge in-process is
# not (1-5s). So ``local`` is MiniLM-class only.
RANK_MODEL: str = os.environ.get("RANK_MODEL") or "cross-encoder/ms-marco-MiniLM-L-6-v2"

# Max sequence length fed to the cross-encoder. Caps per-pair compute so
# long memory content can't blow the latency budget (the A50 latency
# numbers were taken at maxlen 256).
RANK_MAX_LENGTH: int = int(os.environ.get("RANK_MAX_LENGTH", "256"))

# Base URL for the ``remote`` provider (a self-hosted TEI/Cohere-shaped
# reranker sidecar, typically GPU bge). Unset -> in-process only.
RANK_BASE_URL: str | None = os.environ.get("RANK_BASE_URL") or None
RANK_API_KEY: str = os.environ.get("RANK_API_KEY", "")

# Hard per-call deadline for SCORING (the one-time model load is shielded
# from it — see LocalCrossEncoderRanker.rank). A slow rerank degrades to
# first-stage order rather than extending the turn. Default 0.5s: wet-tested
# warm MiniLM predict was ~112-201ms at pool<=20 on fast CPU (slower on prod
# x86), so 0.15s would time out even when warm. 0.5s gives headroom while
# staying far under the turn's multi-second LLM cost. Tune per deployment.
RANK_TIMEOUT_SECONDS: float = float(os.environ.get("RANK_TIMEOUT_SECONDS", "0.5"))

# Retry budget. Default 1 (no retry) — a reranker miss degrades, it does
# not warrant spending more of the turn's latency budget.
RANK_RETRY_ATTEMPTS: int = int(os.environ.get("RANK_RETRY_ATTEMPTS", "1"))
RANK_RETRY_DELAY_S: float = float(os.environ.get("RANK_RETRY_DELAY_S", "0.5"))

# Max candidates re-scored in one call. Bounds cross-encoder compute (the
# MiniLM CPU budget holds only at small pools) and remote payload size.
# Rows beyond this cap keep their first-stage order, appended after the
# re-ranked head.
RANK_CANDIDATE_LIMIT: int = int(os.environ.get("RANK_CANDIDATE_LIMIT", "50"))

# Largest batch the ``remote`` sidecar accepts in ONE /rerank request. The
# provider splits the candidate pool into chunks of this size and issues them
# concurrently, so RANK_CANDIDATE_LIMIT is no longer bounded by what the sidecar
# admits. The coupling moves rather than vanishes, though: the pool size now
# sets FAN-OUT, ``ceil(RANK_CANDIDATE_LIMIT / this)`` concurrent requests per
# search (2 at the defaults).
#
# Default 32 = TEI's own ``--max-client-batch-size`` default, so stock-vs-stock
# works. Raise it to match a sidecar started with a larger cap and a full pool
# goes over in one request again. Cohere-shaped endpoints admit far more per
# call, so 32 is very conservative there — worth raising to at least
# RANK_CANDIDATE_LIMIT if that is the backend.
RANK_REMOTE_MAX_BATCH: int = int(os.environ.get("RANK_REMOTE_MAX_BATCH", "32"))
