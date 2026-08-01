"""Remote reranker — HTTP call to a separate ranking service (sidecar).

Mirrors ``OpenAIEmbeddingProvider`` + ``OPENAI_EMBEDDING_BASE_URL``: the model
lives in its own service (a self-hosted TEI reranker, typically GPU bge, or a
hosted Cohere-style ``/rerank``) and core-api sends it the query + candidate
texts over HTTP. This keeps torch out of the core-api image — the deployment
pattern the stack already uses for the TEI *embedder*.

Contract (TEI-native ``/rerank``, the primary target since the infra runs TEI):

    POST {RANK_BASE_URL}/rerank
    { "query": "<query>", "texts": ["<candidate 0>", "<candidate 1>", ...] }
    -> [ {"index": 1, "score": 0.91}, {"index": 0, "score": 0.42}, ... ]

The response is a ranked list of ``{index, score}`` (index = position in the
input ``texts``). We also accept a Cohere-style wrapper
(``{"results": [{"index", "relevance_score"}]}``) so a Cohere-compatible
endpoint drops in without an adapter. Scores are re-projected back to INPUT
order — the caller (RerankResults) owns the sort.
"""

from __future__ import annotations

import logging

import httpx

from common.ranking.constants import RANK_TIMEOUT_SECONDS
from common.ranking.protocols import RankCandidate

logger = logging.getLogger(__name__)


class RemoteRanker:
    """Reranker backed by a separate HTTP ``/rerank`` service."""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        model: str = "",
        timeout: float = RANK_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        headers = {}
        if api_key:
            # Bearer for Cohere / token-gated TEI; harmless for open TEI.
            headers["Authorization"] = f"Bearer {api_key}"
        # Long-lived pooled client (one rerank call per search). Explicit
        # limits + timeout so a hung sidecar can't ride httpx's 600s default;
        # the service layer's wait_for is the hard outer deadline, this is the
        # transport-level guard.
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            timeout=timeout,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )

    @property
    def provider_name(self) -> str:
        return "remote"

    @property
    def model(self) -> str:
        return self._model or "remote"

    async def aclose(self) -> None:
        """Close the httpx pool cleanly (called on registry cache eviction)."""
        await self._client.aclose()

    async def rank(self, query: str, candidates: list[RankCandidate]) -> list[float]:
        if not candidates:
            return []
        body: dict = {"query": query, "texts": [c.content for c in candidates]}
        resp = await self._client.post("/rerank", json=body)
        resp.raise_for_status()
        data = resp.json()

        # TEI returns a bare list; Cohere-style wraps it in {"results": [...]}.
        results = data.get("results", data) if isinstance(data, dict) else data
        if not isinstance(results, list):
            raise ValueError(f"rerank response not a list: {type(results).__name__}")

        # Re-project the ranked results back onto the INPUT order. Missing
        # indices (a partial response) keep 0.0 — the sort then places them
        # last, which is the safe degradation for a candidate the service
        # dropped rather than crashing the search.
        scores = [0.0] * len(candidates)
        for item in results:
            idx = item["index"]
            if not (0 <= idx < len(candidates)):
                continue
            score = item.get("score")
            if score is None:
                score = item.get("relevance_score")
            scores[idx] = float(score)
        return scores
