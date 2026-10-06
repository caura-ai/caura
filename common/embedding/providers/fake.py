"""Deterministic fake embedding provider for testing/dev.

Hash-based vectors with word-level similarity — texts sharing words
produce vectors with high cosine similarity. No external API keys
required, always succeeds.
"""

from __future__ import annotations

import hashlib
import struct

from common.constants import VECTOR_DIM


def fake_embedding(text: str) -> list[float]:
    """Deterministic fake embedding with word-level similarity.

    Each unique word contributes a deterministic component so texts sharing
    words produce similar vectors (high cosine similarity).
    """
    words = set(text.lower().split())
    result = [0.0] * VECTOR_DIM
    for word in words:
        h = hashlib.sha256(word.encode()).digest()
        raw = h * (VECTOR_DIM // len(h) + 1)
        for i in range(VECTOR_DIM):
            result[i] += struct.unpack_from("b", raw, i)[0] / 128.0
    # Normalize to unit vector
    norm = sum(v * v for v in result) ** 0.5
    if norm > 0:
        result = [v / norm for v in result]
    return result


class FakeEmbeddingProvider:
    """Embedding provider using deterministic hash-based vectors.

    Always succeeds, requires no API keys. Produces vectors that
    preserve word-level similarity.
    """

    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def dedup_scope(self) -> str:
        """Namespace for per-backend failure stats — see ``_stats_by_scope``.

        Model-scoped like the local provider. ``model`` is a constant for
        the stock fake, so every stock instance shares one scope — correct,
        they are one backend. Reading ``self.model`` rather than hardcoding
        it means a subclass that overrides the model (test doubles standing
        in for distinct backends) scopes distinctly too.
        """
        return f"fake:{self.model}"

    @property
    def backend_label(self) -> str:
        """Human-facing identity for log lines — see the OpenAI provider."""
        return f"fake model={self.model}"

    @property
    def model(self) -> str:
        return "fake"

    async def embed(self, text: str) -> list[float]:
        """Generate a deterministic embedding for a single text."""
        return fake_embedding(text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate deterministic embeddings for multiple texts."""
        return [fake_embedding(t) for t in texts]

    # Deliberately does NOT implement ``embed_query`` —
    # ``FakeEmbeddingProvider`` is symmetric by design (deterministic hash).
    # The query path falls back to :meth:`embed` via the
    # ``hasattr(provider, "embed_query")`` check in
    # :func:`common.embedding._service.get_query_embedding`.


class UnconfiguredEmbeddingProvider(FakeEmbeddingProvider):
    """Stand-in for a REAL provider that has no credential to run with.

    Returned by the registry when e.g. ``EMBEDDING_PROVIDER=openai`` (the
    default) has no tenant key, no ``OPENAI_API_KEY`` and no platform
    embedder. It still answers like the fake provider so a keyless deployment
    can run interactive queries, but its vectors must never be STORED: a
    hash vector persisted as a row's embedding is indistinguishable from a
    real one, so once a key is added the row stays invisible to semantic
    search and no backfill can select it. The persisting entry points in
    ``common.embedding._service`` check for this class and return ``None``
    instead, which leaves the row ``embedding=NULL`` — the state the FTS
    admission guard and the embedding backfill already handle.

    ``EMBEDDING_PROVIDER=fake`` is the explicit opt-in and is unaffected: it
    returns the plain :class:`FakeEmbeddingProvider`.
    """
