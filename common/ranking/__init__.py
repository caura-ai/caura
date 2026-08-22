"""Pluggable second-stage ranking (reranking) component.

Mirrors ``common/embedding/`` one-for-one: one provider Protocol, one
registry that dispatches on a config name, one service wrapper that owns
retries + degrade-to-first-stage. The backend — a no-op, an in-process
cross-encoder, or an HTTP call to a separate ranking service — is chosen
by ``RANK_PROVIDER`` exactly like ``EMBEDDING_PROVIDER`` selects an embedder.

Public entrypoint: :func:`common.ranking.get_ranking`.
"""

from __future__ import annotations

from common.ranking._service import get_ranking
from common.ranking.errors import PermanentRankError
from common.ranking.protocols import RankCandidate, RankProvider

__all__ = ["PermanentRankError", "RankCandidate", "RankProvider", "get_ranking"]
