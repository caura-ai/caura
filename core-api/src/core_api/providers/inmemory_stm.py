"""In-memory short-term memory backend.

Stores agent notes and fleet bulletin boards in process memory.
Data is ephemeral — lost on restart. Suitable for single-process OSS
deployments and tests. Production multi-process setups should use Redis.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


class InMemorySTM:
    """Pure-stdlib STM backend backed by plain dicts with TTL and cap."""

    def __init__(
        self,
        notes_max_entries: int = 50,
        bulletin_max_entries: int = 100,
        notes_ttl: int = 86400,
        bulletin_ttl: int = 172800,
    ) -> None:
        self._notes: dict[str, list[tuple[dict[str, Any], float]]] = {}
        self._bulletins: dict[str, list[tuple[dict[str, Any], float]]] = {}
        self._notes_max = notes_max_entries
        self._bulletin_max = bulletin_max_entries
        self._notes_ttl = notes_ttl
        self._bulletin_ttl = bulletin_ttl

    @staticmethod
    def _key(tenant_id: str, scope_id: str) -> str:
        return f"{tenant_id}:{scope_id}"

    def _prune(
        self, store: list[tuple[dict[str, Any], float]], ttl: int
    ) -> list[tuple[dict[str, Any], float]]:
        now = time.monotonic()
        return [(entry, ts) for entry, ts in store if now - ts < ttl]

    # -- notes (per-agent private) -------------------------------------------

    async def get_notes(self, tenant_id: str, agent_id: str, limit: int = 50) -> list[dict[str, Any]]:
        key = self._key(tenant_id, agent_id)
        entries = self._notes.get(key, [])
        entries = self._prune(entries, self._notes_ttl)
        if entries:
            self._notes[key] = entries
        else:
            self._notes.pop(key, None)
        return [e for e, _ts in entries[:limit]]

    async def post_note(self, tenant_id: str, agent_id: str, entry: dict[str, Any]) -> None:
        key = self._key(tenant_id, agent_id)
        store = self._notes.get(key, [])
        store.insert(0, (entry, time.monotonic()))
        store = self._prune(store, self._notes_ttl)
        self._notes[key] = store[: self._notes_max]

    async def clear_notes(self, tenant_id: str, agent_id: str) -> None:
        key = self._key(tenant_id, agent_id)
        self._notes.pop(key, None)

    # -- bulletin boards (per-fleet shared) ----------------------------------

    async def get_bulletin(self, tenant_id: str, fleet_id: str, limit: int = 100) -> list[dict[str, Any]]:
        key = self._key(tenant_id, fleet_id)
        entries = self._bulletins.get(key, [])
        entries = self._prune(entries, self._bulletin_ttl)
        if entries:
            self._bulletins[key] = entries
        else:
            self._bulletins.pop(key, None)
        return [e for e, _ts in entries[:limit]]

    async def post_bulletin(self, tenant_id: str, fleet_id: str, entry: dict[str, Any]) -> None:
        key = self._key(tenant_id, fleet_id)
        store = self._bulletins.get(key, [])
        store.insert(0, (entry, time.monotonic()))
        store = self._prune(store, self._bulletin_ttl)
        self._bulletins[key] = store[: self._bulletin_max]

    async def clear_bulletin(self, tenant_id: str, fleet_id: str) -> None:
        key = self._key(tenant_id, fleet_id)
        self._bulletins.pop(key, None)
