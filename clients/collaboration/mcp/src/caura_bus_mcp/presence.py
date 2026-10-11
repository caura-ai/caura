"""Advertise this MCP server's agent as connected while the stdio server runs.

Without it an agent reachable only through MCP looks offline to ``discover``
(``available_only=true`` by default). The heartbeat refreshes presence at a
third of the TTL Caura returns, and shutdown advertises ``offline``; a crash
leaves the server-side TTL to expire the session.
"""

import asyncio
import logging
import os
import socket
import subprocess
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path

import httpx
from caura_bus_core import AgentConfig
from caura_bus_core.bus import PlatformError
from caura_bus_core.collaboration import PRESENCE_HOST_MAX_LENGTH, Presence
from caura_bus_core.retry import Backoff, transient_status

log = logging.getLogger(__name__)

PRESENCE_ENV_VAR = "CAURA_BUS_MCP_PRESENCE"
DEFAULT_TTL_SECONDS = 45.0
OFFLINE_TIMEOUT_SECONDS = 5.0


def presence_enabled(environ=None) -> bool:
    """Default on; ``0``/``false``/``no``/``off`` disables it."""
    value = (os.environ if environ is None else environ).get(PRESENCE_ENV_VAR, "1")
    return value.strip().lower() not in {"0", "false", "no", "off"}


HOSTNAME_ENV_VAR = "CAURA_BUS_MCP_HOSTNAME"
RUNTIME_ENV_VAR = "CAURA_BUS_RUNTIME"
RUNTIMES = {"claude": "claude-code", "claude-code": "claude-code", "codex": "codex"}
ANCESTOR_DEPTH = 8


def ancestor_runtime(pid: int | None = None, depth: int = ANCESTOR_DEPTH) -> str | None:
    """The nearest ancestor process that is Claude Code or Codex (the MCP host), if any."""
    pid = pid or os.getppid()
    for _ in range(depth):
        if pid <= 1:
            return None
        try:
            out = subprocess.run(
                ["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                capture_output=True,
                text=True,
                check=False,
                timeout=2,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None
        parent, _, command = out.partition(" ")
        name = Path(command.strip()).name.lower()
        if name.startswith("codex"):
            return "codex"
        if name.startswith("claude"):
            return "claude-code"
        try:
            pid = int(parent)
        except ValueError:
            return None
    return None


def detect_runtime(
    environ: Mapping[str, str] | None = None,
    ancestor: Callable[[], str | None] = ancestor_runtime,
) -> str | None:
    """The runtime hosting this stdio server, or None when it cannot be told.

    ``CAURA_BUS_RUNTIME`` wins. Otherwise the markers ``caura-bus join`` reads:
    Claude Code sets ``CLAUDECODE=1`` and Codex sets ``CODEX_THREAD_ID`` /
    ``CODEX_SESSION_ID`` / ``CODEX_SANDBOX``. When none or both are present
    (Codex passes MCP servers a reduced environment) the nearest ancestor
    process decides.
    """
    env = os.environ if environ is None else environ
    explicit = env.get(RUNTIME_ENV_VAR, "").strip().lower()
    if explicit:
        return RUNTIMES.get(explicit, explicit[:64])
    claude = env.get("CLAUDECODE") == "1"
    codex = any(env.get(name) for name in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_SANDBOX"))
    if claude != codex:
        return "claude-code" if claude else "codex"
    return ancestor()


def detect_host(environ: Mapping[str, str] | None = None) -> str | None:
    """This machine's hostname; ``CAURA_BUS_MCP_HOSTNAME`` overrides it, and set empty hides it."""
    env = os.environ if environ is None else environ
    if HOSTNAME_ENV_VAR in env:
        host = env[HOSTNAME_ENV_VAR]
    else:
        try:
            host = socket.gethostname()
        except OSError:
            return None
    host = "".join(c for c in host if c.isprintable()).strip()
    return host[:PRESENCE_HOST_MAX_LENGTH] or None


def presence_profile(
    config: AgentConfig,
    environ: Mapping[str, str] | None = None,
    ancestor: Callable[[], str | None] = ancestor_runtime,
) -> Presence:
    # A fresh public session ID; the private wait session ID is never published.
    return Presence(
        session_id="mcp-" + uuid.uuid4().hex,
        display_name=config.agent.agent_id,
        description=config.agent.description,
        capabilities=config.agent.capabilities,
        host=detect_host(environ),
        runtime=detect_runtime(environ, ancestor),
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
                if (
                    isinstance(exc, PlatformError)
                    and exc.status == 422
                    and (self.profile.host or self.profile.runtime)
                ):
                    # A server that predates host/runtime rejects them; keep presence without.
                    log.debug("Caura presence rejected host/runtime; advertising without them")
                    self.profile = self.profile.model_copy(update={"host": None, "runtime": None})
                    continue
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
