"""Local embedding provider using sentence-transformers.

Runs a local model (default: BAAI/bge-base-en-v1.5) for embedding generation.
The sentence-transformers package is lazy-imported so it remains an optional
dependency.
"""

from __future__ import annotations

import asyncio
import logging

from common.constants import VECTOR_DIM

logger = logging.getLogger(__name__)


class LocalEmbedding:
    """Embedding provider backed by a local sentence-transformers model."""

    def __init__(self, model_name: str = "BAAI/bge-base-en-v1.5") -> None:
        self._model_name = model_name
        self._model = None
        self._load_lock = asyncio.Lock()

    @property
    def provider_name(self) -> str:
        return "local"

    @property
    def model(self) -> str:
        return self._model_name

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _ensure_model(self) -> None:
        if self._model is not None:
            return
        async with self._load_lock:
            if self._model is not None:
                return
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError:
                raise ImportError(
                    "sentence-transformers is required for local embeddings. "
                    "Install it with: pip install sentence-transformers"
                ) from None
            logger.info("Loading local embedding model: %s", self._model_name)
            loop = asyncio.get_running_loop()
            model = await loop.run_in_executor(
                None, lambda: SentenceTransformer(self._model_name)
            )
            test_dim = model.get_sentence_embedding_dimension()
            if test_dim != VECTOR_DIM:
                logger.warning(
                    "Model %s produces %d-dim vectors, expected %d (VECTOR_DIM)",
                    self._model_name,
                    test_dim,
                    VECTOR_DIM,
                )
            self._model = model

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def embed(self, text: str) -> list[float]:
        """Embed a single text string."""
        await self._ensure_model()
        loop = asyncio.get_running_loop()
        vec = await loop.run_in_executor(
            None,
            lambda: self._model.encode(text, normalize_embeddings=True),
        )
        return vec.tolist()

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts."""
        await self._ensure_model()
        loop = asyncio.get_running_loop()
        vecs = await loop.run_in_executor(
            None,
            lambda: self._model.encode(texts, normalize_embeddings=True, batch_size=32),
        )
        return vecs.tolist()

    # Deliberately does NOT implement ``embed_query`` — sentence-
    # transformers models served here (default ``BAAI/bge-base-en-v1.5``)
    # are symmetric encoders with no query-side instruction prefix. The
    # query path falls back to :meth:`embed` via the
    # ``hasattr(provider, "embed_query")`` check in
    # :func:`common.embedding._service.get_query_embedding`.
