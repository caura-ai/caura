"""Deterministic fake ranker for tests/dev.

Hash-based scores with word-level overlap signal — candidates sharing
words with the query score higher — so tests can assert a meaningful
reorder without loading a model or hitting a service. Always succeeds,
no external deps. Mirrors ``FakeEmbeddingProvider``.
"""

from __future__ import annotations

import hashlib

from common.ranking.protocols import RankCandidate


def _fake_score(query: str, content: str) -> float:
    """Deterministic query x content score in [0, 1).

    Rewards word overlap (so relevance is not random), with a small
    hash-derived jitter to break ties deterministically.
    """
    q_words = set(query.lower().split())
    c_words = set(content.lower().split())
    overlap = len(q_words & c_words) / len(q_words) if q_words else 0.0
    h = hashlib.sha256(f"{query}\x00{content}".encode()).digest()
    jitter = h[0] / 255.0 * 0.01  # < overlap granularity, just a tiebreak
    return overlap + jitter


class FakeRanker:
    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake"

    async def rank(self, query: str, candidates: list[RankCandidate]) -> list[float]:
        return [_fake_score(query, c.content) for c in candidates]
