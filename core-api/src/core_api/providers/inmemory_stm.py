"""In-memory short-term memory backend.

Stores agent notes and fleet bulletin boards in process memory.
Data is ephemeral — lost on restart. Suitable for single-process OSS
deployments and tests. Production multi-process setups should use Redis.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from core_api.config import settings

logger = logging.getLogger(__name__)

# How often ``_sweep`` may walk every key. Not configurable: it trades a bounded
# amount of stale memory for a bounded amount of CPU, and neither side of that
# is something an operator has the information to tune better than a constant.
_SWEEP_INTERVAL_SECONDS = 300


class InMemorySTM:
    """Pure-stdlib STM backend backed by plain dicts with TTL and cap.

    The ``post_*`` and ``clear_*`` methods return True unconditionally: a dict assignment has
    no failure mode short of ``MemoryError``, which is not something to report
    as a soft False. They return a bool at all because the protocol requires
    one — see ``STMBackend.post_note`` for why a write has to say whether it
    stored.
    """

    def __init__(
        self,
        notes_max_entries: int = 50,
        bulletin_max_entries: int = 100,
        notes_ttl: int | None = None,
        bulletin_ttl: int | None = None,
    ) -> None:
        # Default to the SETTINGS, as RedisSTM already did. These were literal
        # 86400/172800 — the same numbers ``stm_notes_ttl`` / ``stm_bulletin_ttl``
        # default to, which is exactly why the divergence was invisible: the two
        # agreed until an operator changed a setting, and then the entry expired
        # on the old constant while the write response reported the new value.
        # A backend that ignores the configured TTL and a response that quotes
        # it are the same defect seen from two ends.
        self._notes: dict[str, list[tuple[dict[str, Any], float]]] = {}
        self._bulletins: dict[str, list[tuple[dict[str, Any], float]]] = {}
        self._notes_max = notes_max_entries
        self._bulletin_max = bulletin_max_entries
        self._notes_ttl = notes_ttl if notes_ttl is not None else settings.stm_notes_ttl
        self._bulletin_ttl = bulletin_ttl if bulletin_ttl is not None else settings.stm_bulletin_ttl
        self._last_sweep = time.monotonic()

    @staticmethod
    def _key(tenant_id: str, scope_id: str) -> str:
        return f"{tenant_id}:{scope_id}"

    def _prune(
        self, store: list[tuple[dict[str, Any], float]], ttl: int
    ) -> list[tuple[dict[str, Any], float]]:
        now = time.monotonic()
        return [(entry, ts) for entry, ts in store if now - ts < ttl]

    def _sweep(self) -> None:
        """Drop keys whose every entry has expired.

        ``_prune`` only ever runs against the key being touched, so a key that
        is written and then never read again keeps its list — and its dict
        entry — for the life of the process. Redis has no equivalent leak: its
        keys carry a real TTL and the server reclaims them whether or not
        anyone comes back. This is the in-memory backend paying for that
        itself.

        Time-based rather than on every access: the cost is proportional to the
        number of keys, and paying it per note would make the common path
        scale with total tenants. An interval means the reclaim is late, never
        absent, and lateness costs only memory.

        Called from the reads as well as the writes. Hanging it off writes
        alone left the leak fully open in the process that had stopped
        writing — which is precisely the idle process whose memory nobody is
        watching.
        """
        now = time.monotonic()
        if now - self._last_sweep < _SWEEP_INTERVAL_SECONDS:
            return
        self._last_sweep = now
        for store, ttl in ((self._notes, self._notes_ttl), (self._bulletins, self._bulletin_ttl)):
            # ``v[0]`` is the NEWEST entry — every writer inserts at index 0 —
            # so if that one has expired the whole key has, and the check is
            # O(1) per key. Calling ``_prune`` here instead would allocate and
            # discard a list for every key just to test emptiness, which is the
            # cost this docstring promises it does not pay.
            for key in [k for k, v in store.items() if not v or now - v[0][1] >= ttl]:
                store.pop(key, None)

    # -- notes (per-agent private) -------------------------------------------

    async def get_notes(self, tenant_id: str, agent_id: str, limit: int = 50) -> list[dict[str, Any]]:
        self._sweep()
        key = self._key(tenant_id, agent_id)
        entries = self._notes.get(key, [])
        entries = self._prune(entries, self._notes_ttl)
        if entries:
            self._notes[key] = entries
        else:
            self._notes.pop(key, None)
        return [e for e, _ts in entries[:limit]]

    async def post_note(self, tenant_id: str, agent_id: str, entry: dict[str, Any]) -> bool:
        self._sweep()
        key = self._key(tenant_id, agent_id)
        store = self._notes.get(key, [])
        store.insert(0, (entry, time.monotonic()))
        store = self._prune(store, self._notes_ttl)
        self._notes[key] = store[: self._notes_max]
        return True

    async def clear_notes(self, tenant_id: str, agent_id: str) -> bool:
        key = self._key(tenant_id, agent_id)
        self._notes.pop(key, None)
        return True

    # -- bulletin boards (per-fleet shared) ----------------------------------

    async def get_bulletin(self, tenant_id: str, fleet_id: str, limit: int = 100) -> list[dict[str, Any]]:
        self._sweep()
        key = self._key(tenant_id, fleet_id)
        entries = self._bulletins.get(key, [])
        entries = self._prune(entries, self._bulletin_ttl)
        if entries:
            self._bulletins[key] = entries
        else:
            self._bulletins.pop(key, None)
        return [e for e, _ts in entries[:limit]]

    async def post_bulletin(self, tenant_id: str, fleet_id: str, entry: dict[str, Any]) -> bool:
        self._sweep()
        key = self._key(tenant_id, fleet_id)
        store = self._bulletins.get(key, [])
        store.insert(0, (entry, time.monotonic()))
        store = self._prune(store, self._bulletin_ttl)
        self._bulletins[key] = store[: self._bulletin_max]
        return True

    async def clear_bulletin(self, tenant_id: str, fleet_id: str) -> bool:
        key = self._key(tenant_id, fleet_id)
        self._bulletins.pop(key, None)
        return True
