"""Redis-backed short-term memory backend.

Uses the shared Redis connection from ``core_api.cache``.

READS degrade gracefully: every one is wrapped in try/except and answers with
an empty list when Redis is unavailable, which is an honest answer to "what is
in short-term memory" — nothing the caller can reach.

MUTATIONS — writes and clears alike — report whether they happened, and this
is the distinction the module previously did not make. They were wrapped in the same try/except and returned
None on both paths, so a dropped entry and a stored one were indistinguishable
to the caller; the route above went on to answer 200 with an entry id and a TTL
for a write that never happened. Degrading a read is graceful. Degrading a
write and saying nothing is fabricating a receipt. A dropped CLEAR is the
same lie told about a delete: the caller is told the notes are gone, and they
are still there on the next read.
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

    async def post_note(self, tenant_id: str, agent_id: str, entry: dict[str, Any]) -> bool:
        r = await self._redis()
        if r is None:
            # Not an error condition to Redis — there is simply no connection.
            # It still means the note was not stored, which is what the caller
            # needs to know, so it answers the same False as a failed write.
            logger.warning("RedisSTM.post_note: no Redis connection; note not stored")
            return False
        key = f"stm:notes:{tenant_id}:{agent_id}"
        try:
            pipe = r.pipeline(transaction=False)
            pipe.lpush(key, json.dumps(entry, default=str))
            pipe.ltrim(key, 0, self._notes_max - 1)
            pipe.expire(key, self._notes_ttl)
            await pipe.execute()
            return True
        except Exception:
            # WARNING, not DEBUG: this is a dropped write. At DEBUG it was
            # invisible in every deployment that does not run debug logging,
            # which is the reason the drop went unnoticed.
            logger.warning("RedisSTM.post_note failed; note not stored", exc_info=True)
            return False

    async def clear_notes(self, tenant_id: str, agent_id: str) -> bool:
        r = await self._redis()
        if r is None:
            logger.warning("RedisSTM.clear_notes: no Redis connection; nothing cleared")
            return False
        try:
            await r.delete(f"stm:notes:{tenant_id}:{agent_id}")
            return True
        except Exception:
            logger.warning("RedisSTM.clear_notes failed; nothing cleared", exc_info=True)
            return False

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

    async def post_bulletin(self, tenant_id: str, fleet_id: str, entry: dict[str, Any]) -> bool:
        r = await self._redis()
        if r is None:
            logger.warning("RedisSTM.post_bulletin: no Redis connection; entry not stored")
            return False
        key = f"stm:bul:{tenant_id}:{fleet_id}"
        try:
            pipe = r.pipeline(transaction=False)
            pipe.lpush(key, json.dumps(entry, default=str))
            pipe.ltrim(key, 0, self._bulletin_max - 1)
            pipe.expire(key, self._bulletin_ttl)
            await pipe.execute()
            return True
        except Exception:
            logger.warning("RedisSTM.post_bulletin failed; entry not stored", exc_info=True)
            return False

    async def clear_bulletin(self, tenant_id: str, fleet_id: str) -> bool:
        r = await self._redis()
        if r is None:
            logger.warning("RedisSTM.clear_bulletin: no Redis connection; nothing cleared")
            return False
        try:
            await r.delete(f"stm:bul:{tenant_id}:{fleet_id}")
            return True
        except Exception:
            logger.warning("RedisSTM.clear_bulletin failed; nothing cleared", exc_info=True)
            return False
