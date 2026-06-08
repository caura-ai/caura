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


def _reject_openai_config(msg: str, *args: object) -> None:
    """Record a platform OpenAI-embedding misconfiguration and warn.

    Single-sources the ``"openai-embedding-config"`` error tag shared by
    every config-rejection arm. The caller must ``return`` after calling
    this — the singleton is left ``None`` so the worker fails loud (drops
    embed requests with a visible log) rather than silently embedding
    against the wrong endpoint.
    """
    logger.warning(msg, *args)
    _platform_init_errors.append("openai-embedding-config")


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
            _reject_openai_config(
                "PLATFORM_EMBEDDING_PROVIDER=openai but no PLATFORM_EMBEDDING_API_KEY"
            )
            return

        # Self-hosted OpenAI-compatible endpoint support (TEI, vLLM, ...).
        # The core-worker embeds *documents* exclusively through THIS
        # singleton (core_worker/consumer.py::handle_embed_request →
        # get_platform_embedding), never the registry. Without threading a
        # base_url through here, the worker's write path can only ever reach
        # api.openai.com — so pointing core-api at a self-hosted endpoint via
        # the registry's OPENAI_EMBEDDING_BASE_URL would silently leave the
        # write path (and the stored corpus) on the old vendor. These knobs
        # mirror the registry's in common/embedding/_registry.py so the
        # platform tier can target the same self-hosted endpoint.
        base_url = os.environ.get("PLATFORM_EMBEDDING_BASE_URL") or None
        _send_dim_raw = os.environ.get(
            "PLATFORM_EMBEDDING_SEND_DIMENSIONS", "true"
        ).lower()
        if _send_dim_raw not in ("true", "false"):
            _reject_openai_config(
                "PLATFORM_EMBEDDING_SEND_DIMENSIONS=%r must be 'true' or 'false'",
                _send_dim_raw,
            )
            return
        send_dimensions = _send_dim_raw == "true"

        # The same two guaranteed-failure misconfigurations the registry
        # rejects. Fail loud at init (None singleton → worker drops embed
        # requests with a visible log) rather than 4xx'ing every embed call
        # and silently persisting embedding=NULL.
        if base_url and send_dimensions:
            _reject_openai_config(
                "PLATFORM_EMBEDDING_BASE_URL=%r is set but "
                "PLATFORM_EMBEDDING_SEND_DIMENSIONS is true. Self-hosted "
                "OpenAI-compatible endpoints (TEI, vLLM, ...) reject the "
                "``dimensions=`` kwarg. Set PLATFORM_EMBEDDING_SEND_DIMENSIONS"
                "=false for TEI/vLLM, or unset PLATFORM_EMBEDDING_BASE_URL to "
                "use hosted OpenAI.",
                base_url,
            )
            return
        if not base_url and not send_dimensions:
            _reject_openai_config(
                "PLATFORM_EMBEDDING_SEND_DIMENSIONS=false but "
                "PLATFORM_EMBEDDING_BASE_URL is unset (hosted OpenAI). Hosted "
                "OpenAI returns the model's native dim without the "
                "``dimensions=`` kwarg, which pgvector rejects at write time. "
                "Set PLATFORM_EMBEDDING_SEND_DIMENSIONS=true (or unset it) for "
                "the hosted OpenAI path, or set PLATFORM_EMBEDDING_BASE_URL to "
                "a self-hosted endpoint producing VECTOR_DIM-sized vectors."
            )
            return

        # Matryoshka truncation for models whose native dim > VECTOR_DIM
        # (e.g. Qwen3-Embedding-4B). Constructor re-validates it equals
        # VECTOR_DIM and renormalizes; surface the env-var name on parse error.
        truncate_raw = os.environ.get("PLATFORM_EMBEDDING_TRUNCATE_TO_DIM")
        try:
            truncate_to_dim = int(truncate_raw) if truncate_raw else None
        except ValueError:
            _reject_openai_config(
                "PLATFORM_EMBEDDING_TRUNCATE_TO_DIM=%r must be an integer",
                truncate_raw,
            )
            return

        try:
            embed_model = (
                os.environ.get("PLATFORM_EMBEDDING_MODEL") or OPENAI_EMBEDDING_MODEL
            )
            _platform_embedding = OpenAIEmbeddingProvider(
                api_key=api_key,
                model=embed_model,
                base_url=base_url,
                send_dimensions=send_dimensions,
                truncate_to_dim=truncate_to_dim,
            )
            logger.info(
                "Platform embedding: openai/%s (base_url=%s, send_dimensions=%s)",
                embed_model,
                base_url or "api.openai.com",
                send_dimensions,
            )
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
