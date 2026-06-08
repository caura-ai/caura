"""Provider registry for LLM and infrastructure backends.

CAURA-594: ``get_embedding_provider`` moved to
``common.embedding._registry`` (re-exported from ``common.embedding``)
so ``core-worker`` can use it without depending on ``core-api``. Old
imports of the form ``from core_api.providers import get_embedding_provider``
should be rewritten to ``from common.embedding import get_embedding_provider``.
"""

from core_api.providers._platform import (
    get_platform_embedding,
    get_platform_llm,
)
from core_api.providers._registry import (
    get_conflict_resolver,
    get_identity_resolver,
    get_job_queue,
    get_llm_provider,
    get_stm_backend,
    get_storage_backend,
)

__all__ = [
    "get_conflict_resolver",
    "get_identity_resolver",
    "get_job_queue",
    "get_llm_provider",
    "get_platform_embedding",
    "get_platform_llm",
    "get_stm_backend",
    "get_storage_backend",
]
