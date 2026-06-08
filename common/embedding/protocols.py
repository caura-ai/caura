"""Embedding-provider protocols shared across services.

Two protocols, by design split:

* :class:`EmbeddingProvider` — the symmetric core every provider must
  implement (``embed``, ``embed_batch``, ``provider_name``, ``model``).
  Conformance held by ``Fake``, ``Local``, ``Vertex``, and
  ``OpenAIEmbeddingProvider``.

* :class:`InstructionAwareEmbedder` — the optional extension for models
  trained with asymmetric query/document encoding (Qwen3-Embedding,
  e5-instruct, KaLM). Conformance held by ``OpenAIEmbeddingProvider``
  only — when pointed at an instruction-aware model. Symmetric providers
  deliberately do NOT implement this; callers (see
  :func:`common.embedding._service.get_query_embedding`) ``hasattr``-check
  and fall back to :meth:`EmbeddingProvider.embed`.

Splitting the protocols keeps ``runtime_checkable`` honest:
``isinstance(p, EmbeddingProvider)`` is true for every provider without
forcing a no-op shim onto symmetric ones. The narrower protocol expresses
real capability rather than protocol-conformance ceremony.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Async embedding provider for single and batch text embedding."""

    @property
    def provider_name(self) -> str: ...

    @property
    def model(self) -> str: ...

    async def embed(self, text: str) -> list[float]:
        """Generate an embedding vector for a single text.

        The returned vector size is determined by the provider and model
        configuration. Callers should not assume a specific dimensionality
        — the system-wide ``VECTOR_DIM`` constant and the pgvector column
        definition enforce consistency at the storage layer.
        """
        ...

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embedding vectors for multiple texts in one call.

        Same dimensionality contract as :meth:`embed`.
        """
        ...


@runtime_checkable
class InstructionAwareEmbedder(Protocol):
    """Optional extension for models with asymmetric query/document encoding.

    Instruction-aware embedders (Qwen3-Embedding, e5-instruct, KaLM-Gemma3,
    …) train the query-side encoder to expect a task-description prefix
    that documents do not receive. A provider conforming to this protocol
    must:

    * Apply the prefix on :meth:`embed_query` only — never on the
      document path (:meth:`EmbeddingProvider.embed`).
    * Resolve *instruction* in this order: per-call arg → constructor
      default → no prefix (degenerates to :meth:`embed`).

    Symmetric providers (``Fake``, ``Local``, ``Vertex``,
    OpenAI ``text-embedding-3-small``, ``bge-m3``, ``gte-en-v1.5``) do
    **not** implement this protocol. The query path
    (:func:`common.embedding._service.get_query_embedding`) checks
    ``isinstance(provider, InstructionAwareEmbedder)`` (or, equivalently,
    ``hasattr(provider, "embed_query")``) and falls back to
    :meth:`EmbeddingProvider.embed` when absent.
    """

    async def embed_query(
        self, text: str, instruction: str | None = None
    ) -> list[float]:
        """Generate an embedding vector for a search-side query."""
        ...
