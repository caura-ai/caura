"""Redis-backed short-term memory backend.

Uses the shared Redis connection from ``core_api.cache``.
All operations are wrapped in try/except so STM degrades gracefully
when Redis is unavailable — callers get empty lists instead of errors.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from core_api.config import settings

logger = logging.getLogger(__name__)


class RedisSTM:
    """STM backend backed by Redis lists with TTL and cap."""

    def __init__(
        self,
        notes_max_entries: int = 50,
        bulletin_max_entries: int = 100,
        notes_ttl: int | None = None,
        bulletin_ttl: int | None = None,
    ) -> None:
        self._notes_max = notes_max_entries
        self._bulletin_max = bulletin_max_entries
        self._notes_ttl = notes_ttl if notes_ttl is not None else settings.stm_notes_ttl
        self._bulletin_ttl = bulletin_ttl if bulletin_ttl is not None else settings.stm_bulletin_ttl

    @staticmethod
    async def _redis():
        from core_api.cache import _get_redis

        return await _get_redis()

    # -- notes (per-agent private) -------------------------------------------

    async def get_notes(self, tenant_id: str, agent_id: str, limit: int = 50) -> list[dict[str, Any]]:
        r = await self._redis()
        if r is None:
            return []
        key = f"stm:notes:{tenant_id}:{agent_id}"
        try:
            raw_items = await r.lrange(key, 0, limit - 1)
            return [json.loads(item) for item in raw_items]
        except Exception:
            logger.debug("RedisSTM.get_notes failed", exc_info=True)
            return []

    async def post_note(self, tenant_id: str, agent_id: str, entry: dict[str, Any]) -> None:
        r = await self._redis()
        if r is None:
            return
        key = f"stm:notes:{tenant_id}:{agent_id}"
        try:
            pipe = r.pipeline(transaction=False)
            pipe.lpush(key, json.dumps(entry, default=str))
            pipe.ltrim(key, 0, self._notes_max - 1)
            pipe.expire(key, self._notes_ttl)
            await pipe.execute()
        except Exception:
            logger.debug("RedisSTM.post_note failed", exc_info=True)

    async def clear_notes(self, tenant_id: str, agent_id: str) -> None:
        r = await self._redis()
        if r is None:
            return
        try:
            await r.delete(f"stm:notes:{tenant_id}:{agent_id}")
        except Exception:
            logger.debug("RedisSTM.clear_notes failed", exc_info=True)

    # -- bulletin (per-fleet shared) -----------------------------------------

    async def get_bulletin(self, tenant_id: str, fleet_id: str, limit: int = 100) -> list[dict[str, Any]]:
        r = await self._redis()
        if r is None:
            return []
        key = f"stm:bul:{tenant_id}:{fleet_id}"
        try:
            raw_items = await r.lrange(key, 0, limit - 1)
            return [json.loads(item) for item in raw_items]
        except Exception:
            logger.debug("RedisSTM.get_bulletin failed", exc_info=True)
            return []

    async def post_bulletin(self, tenant_id: str, fleet_id: str, entry: dict[str, Any]) -> None:
        r = await self._redis()
        if r is None:
            return
        key = f"stm:bul:{tenant_id}:{fleet_id}"
        try:
            pipe = r.pipeline(transaction=False)
            pipe.lpush(key, json.dumps(entry, default=str))
            pipe.ltrim(key, 0, self._bulletin_max - 1)
            pipe.expire(key, self._bulletin_ttl)
            await pipe.execute()
        except Exception:
            logger.debug("RedisSTM.post_bulletin failed", exc_info=True)

    async def clear_bulletin(self, tenant_id: str, fleet_id: str) -> None:
        r = await self._redis()
        if r is None:
            return
        try:
            await r.delete(f"stm:bul:{tenant_id}:{fleet_id}")
        except Exception:
            logger.debug("RedisSTM.clear_bulletin failed", exc_info=True)
