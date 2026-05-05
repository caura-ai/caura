"""Platform default embedding-provider singleton.

Pre-built at startup from ``PLATFORM_EMBEDDING_*`` env vars. Returned by
:func:`common.embedding.get_embedding_provider` when a tenant has no
credentials configured — tier 2 in the three-tier resolution:

    Tenant key  →  Platform singleton  →  FakeEmbeddingProvider

Security: keys are sealed into the singleton at construction time and
never enter tenant-configurable code paths.

Env-driven (no service-config dependency) so both core-api and
core-worker initialise the same singleton from the same env shape.
"""

from __future__ import annotations

import logging
import os

from common.embedding.constants import OPENAI_EMBEDDING_MODEL
from common.embedding.protocols import EmbeddingProvider
from common.embedding.providers.openai import OpenAIEmbeddingProvider
from common.provider_names import ProviderName

logger = logging.getLogger(__name__)

_platform_embedding: EmbeddingProvider | None = None
_platform_init_errors: list[str] = []


def init_platform_embedding() -> None:
    """Build the singleton from ``PLATFORM_EMBEDDING_*`` env vars.

    Idempotent — call once during service lifespan startup. Subsequent
    calls reset and rebuild the singleton (useful for tests).
    """
    global _platform_embedding
    _platform_embedding = None
    _platform_init_errors.clear()

    provider = os.environ.get("PLATFORM_EMBEDDING_PROVIDER", "")
    if not provider:
        # No platform default configured — tenants without their own
        # keys will fall through to FakeEmbeddingProvider.
        return

    if provider == ProviderName.OPENAI:
        api_key = os.environ.get("PLATFORM_EMBEDDING_API_KEY", "")
        if not api_key:
            logger.warning(
                "PLATFORM_EMBEDDING_PROVIDER=openai but no PLATFORM_EMBEDDING_API_KEY"
            )
            _platform_init_errors.append("openai-embedding-config")
            return
        try:
            embed_model = (
                os.environ.get("PLATFORM_EMBEDDING_MODEL") or OPENAI_EMBEDDING_MODEL
            )
            _platform_embedding = OpenAIEmbeddingProvider(
                api_key=api_key, model=embed_model
            )
            logger.info("Platform embedding: openai/%s", embed_model)
        except Exception:
            logger.exception("Failed to initialize platform OpenAI embedding provider")
            _platform_init_errors.append("openai-embedding")
        return

    if provider == ProviderName.VERTEX:
        # Vertex embeddings were removed (CAURA-333): the SDK call never passed
        # ``output_dimensionality`` so every write 4xx'd against pgvector's
        # 1024-dim column. OSS users wanting non-OpenAI embeddings can implement
        # their own ``EmbeddingProvider`` subclass at their own risk; the schema
        # constraint (``VECTOR(VECTOR_DIM)``) still applies.
        # NOT appended to _platform_init_errors: that list surfaces as
        # health="degraded" on /status, and we don't want to block blue-green
        # health gates for operators whose stale env still says vertex. The
        # warning log + None singleton (== unconfigured) is the safe outcome.
        logger.warning(
            "PLATFORM_EMBEDDING_PROVIDER=vertex is no longer supported. "
            "Use PLATFORM_EMBEDDING_PROVIDER=openai, or supply your own "
            "EmbeddingProvider implementation."
        )
        return

    logger.warning(
        "Unknown PLATFORM_EMBEDDING_PROVIDER=%r — no platform embedding will be configured",
        provider,
    )
    _platform_init_errors.append("unknown-embedding-provider")


def get_platform_embedding() -> EmbeddingProvider | None:
    """Return the platform embedding singleton, or ``None`` if unset."""
    return _platform_embedding


def get_platform_init_errors() -> list[str]:
    """Provider names that failed during the most recent ``init_platform_embedding`` call."""
    return list(_platform_init_errors)
