"""STM service — orchestration between routes and STM backend."""

from __future__ import annotations

import logging
from typing import Any

from core_api.config import settings
from core_api.protocols import STMBackend

logger = logging.getLogger(__name__)

_stm_instance: STMBackend | None = None


def get_stm_backend_instance() -> STMBackend:
    """Return (and lazily create) the singleton STM backend."""
    global _stm_instance
    if _stm_instance is None:
        from core_api.providers import get_stm_backend

        _stm_instance = get_stm_backend(settings.stm_backend)
        # In-memory STM on more than one worker is refused at startup
        # (``Settings._refuse_in_memory_stm_across_workers``, L-35).
        logger.info("STM backend initialised: %s", settings.stm_backend)
    return _stm_instance


async def read_notes(tenant_id: str, agent_id: str, limit: int = 50) -> list[dict[str, Any]]:
    stm = get_stm_backend_instance()
    return await stm.get_notes(tenant_id, agent_id, limit=limit)


async def read_bulletin(tenant_id: str, fleet_id: str, limit: int = 100) -> list[dict[str, Any]]:
    stm = get_stm_backend_instance()
    return await stm.get_bulletin(tenant_id, fleet_id, limit=limit)


async def clear_notes(tenant_id: str, agent_id: str) -> bool:
    stm = get_stm_backend_instance()
    return await stm.clear_notes(tenant_id, agent_id)


async def clear_bulletin(tenant_id: str, fleet_id: str) -> bool:
    stm = get_stm_backend_instance()
    return await stm.clear_bulletin(tenant_id, fleet_id)


async def promote(
    content: str,
    tenant_id: str,
    agent_id: str,
    fleet_id: str | None = None,
    memory_type: str | None = None,
    visibility: str | None = None,
) -> Any:
    """Promote STM content to LTM via create_memory(write_mode='fast')."""
    from core_api.schemas import MemoryCreate
    from core_api.services.memory_service import create_memory

    data = MemoryCreate(
        tenant_id=tenant_id,
        agent_id=agent_id,
        fleet_id=fleet_id,
        content=content,
        memory_type=memory_type,
        visibility=visibility,
        write_mode="fast",
        metadata={"promoted_from": "stm"},
    )
    return await create_memory(data)
