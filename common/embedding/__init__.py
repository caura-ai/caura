"""Embedding adapter shared across services.

Public surface:

* :class:`~common.embedding.protocols.EmbeddingProvider` — async protocol
  every provider implements.
* :class:`~common.embedding.protocols.InstructionAwareEmbedder` — optional
  protocol for models with asymmetric query/document encoding (Qwen3-class).
* :func:`get_embedding` / :func:`get_embeddings_batch` /
  :func:`get_query_embedding` — service-level entrypoints with retry.
  ``get_query_embedding`` routes through the instruction-aware path when
  the resolved provider implements ``InstructionAwareEmbedder``, else
  falls back to :func:`get_embedding`'s symmetric behaviour. Backwards-
  compatible with the prior ``core_api.services.embedding`` module.
* :func:`get_embedding_provider` — factory.
* :func:`init_platform_embedding` / :func:`get_platform_embedding` —
  platform-tier singleton, initialised once at service startup from
  ``PLATFORM_EMBEDDING_*`` env vars.
* :func:`fake_embedding`, :class:`FakeEmbeddingProvider` — deterministic
  hash-based vectors for tests/dev.

Reads provider settings from environment variables (``OPENAI_API_KEY``,
``EMBEDDING_PROVIDER``, ``OPENAI_EMBEDDING_MODEL``, etc.) — no
dependency on any service-specific config module — so both core-api
(tenant-aware) and core-worker (platform-only) can share the same
implementation.

CAURA-594 full form: extracted from ``core_api.services.embedding`` and
``core_api.providers.*`` so that ``core-worker`` can subscribe to
embed-request events without depending on ``core-api``.
"""

from __future__ import annotations

from common.embedding._platform import (
    get_platform_embedding,
    get_platform_init_errors,
    init_platform_embedding,
)
from common.embedding._registry import get_embedding_provider
from common.embedding._service import (
    get_embedding,
    get_embeddings_batch,
    get_query_embedding,
)
from common.embedding.protocols import EmbeddingProvider, InstructionAwareEmbedder
from common.embedding.providers.fake import (
    FakeEmbeddingProvider,
    fake_embedding,
)

__all__ = [
    "EmbeddingProvider",
    "FakeEmbeddingProvider",
    "InstructionAwareEmbedder",
    "fake_embedding",
    "get_embedding",
    "get_embedding_provider",
    "get_embeddings_batch",
    "get_platform_embedding",
    "get_platform_init_errors",
    "get_query_embedding",
    "init_platform_embedding",
]
