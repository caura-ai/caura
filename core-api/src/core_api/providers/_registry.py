"""Provider registry: construct LLM providers and infrastructure backends by name.

CAURA-595: ``get_llm_provider`` moved to ``common.llm.registry``
(re-exported here) so core-worker can construct LLM providers without
importing core-api.

Four sibling factories used to live here — ``get_storage_backend``,
``get_job_queue``, ``get_identity_resolver`` and ``get_conflict_resolver``
— along with the SqliteBackend, InProcessQueue, ConfigIdentity and
ManualResolver they returned. Nothing outside their own tests called
them, and unlike ``get_stm_backend`` no setting selected them: there was
no ``storage_backend`` / ``job_queue`` / ``identity_resolver`` /
``conflict_resolver`` name anywhere in ``core_api.config``, so the only
way to reach one was to call the factory by hand. They were deleted
rather than kept as extension points: an extension point nothing selects
is indistinguishable from dead code, and it had already drifted (the
SQLite backend carried its own ``memories`` DDL, maintained by no
migration).
"""

from __future__ import annotations

from common.llm.registry import get_llm_provider
from core_api.protocols import STMBackend

__all__ = [
    "get_llm_provider",
    "get_stm_backend",
]


def get_stm_backend(name: str = "memory", **kwargs: object) -> STMBackend:
    """Construct an STM backend by name.

    Supported names: ``"memory"``, ``"redis"`` — selected by
    ``settings.stm_backend`` in :func:`core_api.services.stm_service.
    get_stm_backend_instance`.
    """
    if name == "memory":
        from core_api.providers.inmemory_stm import InMemorySTM

        return InMemorySTM(**kwargs)
    if name == "redis":
        from core_api.providers.redis_stm import RedisSTM

        return RedisSTM(**kwargs)
    raise ValueError(f"Unknown STM backend: {name}")
