"""No-op ranker — the default, a TRUE identity on first-stage order.

Ships the component dark: ``RANK_PROVIDER=noop`` (the default) must leave
the result order byte-for-byte identical to what scored-search produced.

Subtlety: the caller sorts candidates by the returned scores (descending).
First-stage rows arrive ordered by the composite ``score`` (SQL ORDER BY
score), which is NOT the same as ``similarity`` order once boosts diverge.
So returning ``[c.similarity]`` would silently re-order the pool by cosine
— a behaviour change, not a no-op. Instead we return a strictly
descending score by input position, so the caller's sort reproduces the
exact input order regardless of similarity/score.
"""

from __future__ import annotations

from common.ranking.protocols import RankCandidate


class NoopRanker:
    @property
    def provider_name(self) -> str:
        return "noop"

    @property
    def model(self) -> str:
        return "noop"

    async def rank(self, query: str, candidates: list[RankCandidate]) -> list[float]:
        # Descending by position → caller's ``sort(reverse=True)`` keeps the
        # input order exactly. A true identity, independent of similarity.
        n = len(candidates)
        return [float(n - i) for i in range(n)]
