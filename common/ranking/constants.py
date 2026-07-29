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

# Provider selection default. ``noop`` = identity (keep first-stage
# order) so the component ships dark: zero behaviour change until a
# deployment or tenant opts in. Mirrors ``EMBEDDING_PROVIDER``.
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

# Hard per-call deadline. Recall is latency-critical: a slow rerank must
# degrade to first-stage order, not extend the turn. Default 150ms matches
# the small-pool MiniLM budget from the latency spike.
RANK_TIMEOUT_SECONDS: float = float(os.environ.get("RANK_TIMEOUT_SECONDS", "0.15"))

# Retry budget. Default 1 (no retry) — a reranker miss degrades, it does
# not warrant spending more of the turn's latency budget.
RANK_RETRY_ATTEMPTS: int = int(os.environ.get("RANK_RETRY_ATTEMPTS", "1"))
RANK_RETRY_DELAY_S: float = float(os.environ.get("RANK_RETRY_DELAY_S", "0.5"))

# Max candidates re-scored in one call. Bounds cross-encoder compute (the
# MiniLM CPU budget holds only at small pools) and remote payload size.
# Rows beyond this cap keep their first-stage order, appended after the
# re-ranked head.
RANK_CANDIDATE_LIMIT: int = int(os.environ.get("RANK_CANDIDATE_LIMIT", "50"))
