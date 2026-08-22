"""Rank-provider protocol shared across the search path.

Mirrors ``common/embedding/protocols.py``: one ``@runtime_checkable``
Protocol every backend implements. A provider is *pure* — it turns a
query + candidate pool into one relevance score per candidate and has no
DB access, no writes, and no side effects. The pipeline step owns the
sort + trim, exactly as the embedding caller owns what it does with the
returned vector.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class RankCandidate:
    """One first-stage result handed to a ranker for re-scoring.

    ``content`` is the text a cross-encoder / remote service scores
    against the query. ``similarity`` is the first-stage relevance so a
    provider *can* blend rather than replace (the ``noop`` provider just
    returns it). ``features`` carries the other scored-search signals
    (freshness, recall_count, memory_type, status, ts_valid_*) so a future
    blending/learned ranker has them without a contract change — today's
    providers ignore it (design decision: start with ``content`` only).
    """

    id: str
    content: str
    similarity: float
    features: dict = field(default_factory=dict)


@runtime_checkable
class RankProvider(Protocol):
    """Async reranker: query + candidates → one score per candidate.

    Implementations must return scores in the SAME order as the input
    candidates (``len(scores) == len(candidates)``). Pure: no DB, no
    writes, no side effects — the caller sorts and trims.
    """

    @property
    def provider_name(self) -> str: ...

    @property
    def model(self) -> str: ...

    async def rank(self, query: str, candidates: list[RankCandidate]) -> list[float]:
        """Return one relevance score per candidate, input order preserved."""
        ...
