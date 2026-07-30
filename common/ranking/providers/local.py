"""Local in-process cross-encoder ranker (MiniLM), mirrors ``LocalEmbedding``.

Lazy-loads a sentence-transformers ``CrossEncoder`` under an
``asyncio.Lock`` and scores (query, content) pairs in a thread executor
(``predict`` is sync CPU work). ``sentence-transformers`` is lazy-imported
so it stays an optional dependency — if it is absent the load raises
``ImportError``, which the service layer treats as a provider failure and
degrades to first-stage order.

Viable only at small pools: MiniLM-L6 (22M) is ~50-150ms CPU at pool<=20
(A50 latency spike); a large model (bge-base, 278M) is NOT CPU-viable
in-process (1-5s) and belongs behind the ``remote`` provider.
"""

from __future__ import annotations

import asyncio
import logging

from common.ranking.constants import RANK_MAX_LENGTH, RANK_MODEL
from common.ranking.protocols import RankCandidate

logger = logging.getLogger(__name__)


class LocalCrossEncoderRanker:
    """Reranker backed by a local sentence-transformers CrossEncoder."""

    def __init__(
        self, model_name: str = RANK_MODEL, max_length: int = RANK_MAX_LENGTH
    ) -> None:
        self._model_name = model_name
        self._max_length = max_length
        self._model = None
        self._load_lock = asyncio.Lock()

    @property
    def provider_name(self) -> str:
        return "local"

    @property
    def model(self) -> str:
        return self._model_name

    async def _ensure_model(self) -> None:
        if self._model is not None:
            return
        async with self._load_lock:
            if self._model is not None:
                return
            try:
                from sentence_transformers import CrossEncoder
            except ImportError:
                raise ImportError(
                    "sentence-transformers is required for the local reranker "
                    "(RANK_PROVIDER=local). Install it with: "
                    "pip install sentence-transformers"
                ) from None
            logger.info("Loading local cross-encoder model: %s", self._model_name)
            loop = asyncio.get_running_loop()
            self._model = await loop.run_in_executor(
                None,
                lambda: CrossEncoder(self._model_name, max_length=self._max_length),
            )

    async def rank(self, query: str, candidates: list[RankCandidate]) -> list[float]:
        if not candidates:
            return []
        # Shield the one-time model load from the caller's per-call deadline.
        # The service wraps ``rank`` in ``asyncio.wait_for(RANK_TIMEOUT_SECONDS)``;
        # a cold load takes seconds, so without the shield the first call's
        # timeout cancels the load mid-flight and DISCARDS it (``self._model``
        # stays None) — every subsequent call restarts the load and the
        # provider never warms up, permanently falling back to first-stage
        # order (verified by wet test). ``shield`` lets the load run to
        # completion + cache even when this call is cancelled; the caller
        # still degrades for the cold call(s), then warm calls score normally.
        await asyncio.shield(self._ensure_model())
        pairs = [(query, c.content) for c in candidates]
        loop = asyncio.get_running_loop()
        # ``predict`` is synchronous, CPU-bound batched inference — run it
        # off the event loop so a rerank never blocks other requests.
        scores = await loop.run_in_executor(None, lambda: self._model.predict(pairs))
        return [float(s) for s in scores]
