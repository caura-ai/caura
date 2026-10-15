"""Advertise this MCP server's agent as connected while the stdio server runs.

Without it an agent reachable only through MCP looks offline to ``discover``
(``available_only=true`` by default). The heartbeat refreshes presence at a
third of the TTL Caura returns, and shutdown advertises ``offline``; a crash
leaves the server-side TTL to expire the session.

While the runtime's waiting-for-user mark is set (a file the caura broker's
Claude Code hooks write at a permission prompt and remove on the next tool
result, prompt or stop), the session is advertised as ``waiting_for_user``.
"""

import asyncio
import logging
import os
import socket
import subprocess
import time
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
WAITING_FILE_ENV_VAR = "CAURA_BUS_WAITING_FILE"
WAITING_STATUS = "waiting_for_user"
# How soon a set or cleared mark reaches the platform.
WAITING_POLL_SECONDS = 1.0


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


def waiting_file(environ: Mapping[str, str] | None = None) -> str | None:
    return (os.environ if environ is None else environ).get(WAITING_FILE_ENV_VAR) or None


class PresenceHeartbeat:
    def __init__(self, bus, profile: Presence, waiting_file: str | None = None):
        self.bus = bus
        self.profile = profile
        self.waiting_file = waiting_file
        # A mark older than this server was left by an earlier session. Whole
        # seconds: some file systems store mtime at one-second resolution.
        self.started = float(int(time.time()))
        self.waiting_supported = True
        self.task: asyncio.Task | None = None
        self.revoked = False

    def waiting(self) -> bool:
        if not self.waiting_file or not self.waiting_supported:
            return False
        try:
            return os.stat(self.waiting_file).st_mtime >= self.started
        except OSError:
            return False

    def current(self) -> Presence:
        if self.waiting():
            return self.profile.model_copy(update={"status": WAITING_STATUS})
        return self.profile

    async def pause(self, seconds: float) -> None:
        """Sleep until the next heartbeat, or until the waiting mark changes."""
        if not self.waiting_file:
            await asyncio.sleep(seconds)
            return
        advertised = self.waiting()
        loop = asyncio.get_running_loop()
        end = loop.time() + seconds
        while (left := end - loop.time()) > 0:
            await asyncio.sleep(min(WAITING_POLL_SECONDS, left))
            if self.waiting() != advertised:
                return

    def start(self) -> None:
        self.task = asyncio.create_task(self.run())

    async def run(self) -> None:
        backoff = Backoff()
        while True:
            profile = self.current()
            try:
                result = await self.bus.advertise(profile)
            except (httpx.TransportError, PlatformError) as exc:
                if isinstance(exc, PlatformError) and exc.status == 422 and profile.status == WAITING_STATUS:
                    # A server that predates waiting_for_user rejects it; keep plain presence.
                    log.debug("Caura presence rejected waiting_for_user; advertising without it")
                    self.waiting_supported = False
                    continue
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
            await self.pause(heartbeat_interval(result))

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
