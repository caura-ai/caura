"""Advertise this MCP server's agent as connected while the stdio server runs.

Without it an agent reachable only through MCP looks offline to ``discover``
(``available_only=true`` by default). The heartbeat refreshes presence at a
third of the TTL Caura returns, and shutdown advertises ``offline``; a crash
leaves the server-side TTL to expire the session.
"""

import asyncio
import logging
import os
import uuid
from contextlib import suppress

import httpx
from caura_bus_core import AgentConfig
from caura_bus_core.bus import PlatformError
from caura_bus_core.collaboration import Presence
from caura_bus_core.retry import Backoff, transient_status

log = logging.getLogger(__name__)

PRESENCE_ENV_VAR = "CAURA_BUS_MCP_PRESENCE"
DEFAULT_TTL_SECONDS = 45.0
OFFLINE_TIMEOUT_SECONDS = 5.0


def presence_enabled(environ=None) -> bool:
    """Default on; ``0``/``false``/``no``/``off`` disables it."""
    value = (os.environ if environ is None else environ).get(PRESENCE_ENV_VAR, "1")
    return value.strip().lower() not in {"0", "false", "no", "off"}


def presence_profile(config: AgentConfig) -> Presence:
    # A fresh public session ID; the private wait session ID is never published.
    return Presence(
        session_id="mcp-" + uuid.uuid4().hex,
        display_name=config.agent.agent_id,
        description=config.agent.description,
        capabilities=config.agent.capabilities,
    )


def heartbeat_interval(result) -> float:
    ttl = result.get("ttl_seconds") if isinstance(result, dict) else None
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or ttl <= 0:
        ttl = DEFAULT_TTL_SECONDS
    return max(1.0, ttl / 3)


class PresenceHeartbeat:
    def __init__(self, bus, profile: Presence):
        self.bus = bus
        self.profile = profile
        self.task: asyncio.Task | None = None
        self.revoked = False

    def start(self) -> None:
        self.task = asyncio.create_task(self.run())

    async def run(self) -> None:
        backoff = Backoff()
        while True:
            try:
                result = await self.bus.advertise(self.profile)
            except (httpx.TransportError, PlatformError) as exc:
                if isinstance(exc, PlatformError) and not transient_status(exc.status):
                    # Revoked or rejected: retrying cannot help. The peer tool keeps working.
                    self.revoked = exc.status in {401, 403}
                    log.warning("Caura presence stopped after platform status %s", exc.status)
                    return
                log.debug("Caura presence unavailable; retrying")
                await backoff.sleep()
                continue
            backoff.reset()
            await asyncio.sleep(heartbeat_interval(result))

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self.task
        if self.revoked:
            return
        # Best effort: a failed offline write still expires with the TTL.
        with suppress(Exception):
            async with asyncio.timeout(OFFLINE_TIMEOUT_SECONDS):
                await self.bus.advertise(self.profile.model_copy(update={"status": "offline"}))
