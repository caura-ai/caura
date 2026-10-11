"""Agent sessions seen by this core-api process (value metering).

Every MCP tool call and mapped REST call that carries a session
(``core_api.session_context``) touches it here: first and last seen, the calls
it made, and the calls since its previous write. Flushed every 15 s, one
request per tenant (each bound to that tenant), as additive upserts — so
several instances seeing one session all land.

``calls_since_write`` is per process: a session spread across instances shows
fewer calls than it made, which is why ``origin_calls`` is documented as a
lower bound.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

FLUSH_INTERVAL_SECONDS = 15.0
_MAX_TRACKED = 50_000


@dataclass
class _Seen:
    agent_id: str
    source: str
    first_seen: datetime
    last_seen: datetime
    calls: int = 0
    unflushed_calls: int = 0
    calls_since_write: int = 0


class SessionTracker:
    def __init__(self, flush_interval: float = FLUSH_INTERVAL_SECONDS) -> None:
        self._lock = threading.Lock()
        self._seen: dict[tuple[str, str], _Seen] = {}
        self._dirty: set[tuple[str, str]] = set()
        self._interval = flush_interval
        self._task: asyncio.Task | None = None

    def touch(
        self, tenant_id: str | None, session_id: str | None, source: str | None, agent_id: str | None
    ) -> None:
        if not tenant_id or not session_id:
            return
        now = datetime.now(UTC)
        key = (tenant_id, session_id)
        with self._lock:
            seen = self._seen.get(key)
            if seen is None:
                if len(self._seen) >= _MAX_TRACKED:
                    # Forget the stalest: its totals are already flushed, so only
                    # its in-process "since last write" count is lost.
                    oldest = min(self._seen, key=lambda k: self._seen[k].last_seen)
                    self._seen.pop(oldest, None)
                    self._dirty.discard(oldest)
                seen = self._seen[key] = _Seen(agent_id or "", source or "", now, now)
            seen.last_seen = now
            seen.calls += 1
            seen.unflushed_calls += 1
            seen.calls_since_write += 1
            if agent_id and not seen.agent_id:
                seen.agent_id = agent_id
            self._dirty.add(key)

    def take_calls_since_write(self, tenant_id: str, session_id: str) -> int | None:
        """The calls this process saw since the session's previous write, reset for the next one."""
        with self._lock:
            seen = self._seen.get((tenant_id, session_id))
            if seen is None:
                return None
            calls, seen.calls_since_write = seen.calls_since_write, 0
            return calls

    def _drain(self) -> dict[str, list[dict]]:
        by_tenant: dict[str, list[dict]] = {}
        with self._lock:
            for key in self._dirty:
                seen = self._seen.get(key)
                if seen is None:
                    continue
                by_tenant.setdefault(key[0], []).append(
                    {
                        "session_id": key[1],
                        "agent_id": seen.agent_id,
                        "source": seen.source,
                        "first_seen": seen.first_seen.isoformat(),
                        "last_seen": seen.last_seen.isoformat(),
                        "calls": seen.unflushed_calls,
                    }
                )
                seen.unflushed_calls = 0
            self._dirty.clear()
        return by_tenant

    async def flush(self) -> int:
        from core_api.clients.storage_client import get_storage_client

        sc = get_storage_client()
        sent = 0
        for tenant_id, rows in self._drain().items():
            try:
                await sc.value_touch_sessions(tenant_id, rows)
                sent += len(rows)
            except Exception:
                # Lost calls only under-count; the session itself is recorded
                # again by its next call or by its next write's origin.
                logger.warning("session tracker: flush failed for one tenant", exc_info=True)
        return sent

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self.flush()
            except Exception:  # pragma: no cover - defensive
                logger.exception("session tracker: flush loop error")

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._loop(), name="session-tracker-flush")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        with contextlib.suppress(Exception):
            await self.flush()


_tracker: SessionTracker | None = None


def install() -> SessionTracker:
    global _tracker
    _tracker = SessionTracker()
    _tracker.start()
    return _tracker


async def uninstall(tracker: SessionTracker) -> None:
    global _tracker
    await tracker.stop()
    if _tracker is tracker:
        _tracker = None


def touch_current(tenant_id: str | None, agent_id: str | None = None) -> None:
    """Touch the request's session, if it has one. Never raises."""
    if _tracker is None:
        return
    try:
        from core_api.session_context import current_session_id, current_session_source

        _tracker.touch(tenant_id, current_session_id(), current_session_source(), agent_id)
    except Exception:  # pragma: no cover - defensive
        logger.warning("session tracker: touch failed", exc_info=True)


def take_calls_since_write(tenant_id: str, session_id: str) -> int | None:
    return _tracker.take_calls_since_write(tenant_id, session_id) if _tracker is not None else None
