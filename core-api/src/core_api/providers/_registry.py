"""Provider registry: construct LLM providers and infrastructure backends by name.

CAURA-595: ``get_llm_provider`` moved to ``common.llm.registry``
(re-exported here) so core-worker can construct LLM providers without
importing core-api. Infrastructure backend factories (storage, job
queue, identity, conflict resolver, STM) stay here — those are
core-api-only concerns.
"""

from __future__ import annotations

from common.llm.registry import get_llm_provider
from core_api.protocols import (
    ConflictResolver,
    IdentityResolver,
    JobQueue,
    STMBackend,
    StorageBackend,
)

__all__ = [
    "get_conflict_resolver",
    "get_identity_resolver",
    "get_job_queue",
    "get_llm_provider",
    "get_stm_backend",
    "get_storage_backend",
]


# ---------------------------------------------------------------------------
# Infrastructure backend factories
# ---------------------------------------------------------------------------


def get_storage_backend(name: str = "sqlite", **kwargs: object) -> StorageBackend:
    """Construct a storage backend by name.

    Supported names: ``"sqlite"``.
    """
    if name == "sqlite":
        from core_api.providers.sqlite_backend import SqliteBackend

        return SqliteBackend(**kwargs)
    raise ValueError(f"Unknown storage backend: {name}")


def get_job_queue(name: str = "inprocess") -> JobQueue:
    """Construct a job queue by name.

    Supported names: ``"inprocess"``.
    """
    if name == "inprocess":
        from core_api.providers.inprocess_queue import InProcessQueue

        return InProcessQueue()
    raise ValueError(f"Unknown job queue: {name}")


def get_identity_resolver(name: str = "config", **kwargs: object) -> IdentityResolver:
    """Construct an identity resolver by name.

    Supported names: ``"config"``.
    """
    if name == "config":
        from core_api.providers.config_identity import ConfigIdentity

        return ConfigIdentity(**kwargs)
    raise ValueError(f"Unknown identity resolver: {name}")


def get_conflict_resolver(name: str = "manual") -> ConflictResolver:
    """Construct a conflict resolver by name.

    Supported names: ``"manual"``.
    """
    if name == "manual":
        from core_api.providers.manual_resolver import ManualResolver

        return ManualResolver()
    raise ValueError(f"Unknown conflict resolver: {name}")


def get_stm_backend(name: str = "memory", **kwargs: object) -> STMBackend:
    """Construct an STM backend by name.

    Supported names: ``"memory"``.
    """
    if name == "memory":
        from core_api.providers.inmemory_stm import InMemorySTM

        return InMemorySTM(**kwargs)
    if name == "redis":
        from core_api.providers.redis_stm import RedisSTM

        return RedisSTM(**kwargs)
    raise ValueError(f"Unknown STM backend: {name}")
