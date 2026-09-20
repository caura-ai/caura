"""In-process counter of the client families that talked to this server.

``get_auth_context`` (REST) and the MCP auth middleware call :func:`record`
after a request authenticates. The counter is a dict of ints keyed on a fixed
family set; unknown agents fold into ``other``. Only one prefix match per
request, and nothing at all while the heartbeat is off (:func:`enable` is
called by the sender when the policy says on, so a disabled install never
increments anything).

Families are matched on the ``User-Agent`` prefix the SDKs send
(``caura-client-python/1.0.2``, ``caura-rail-node/1.0.1`` ...). MCP requests
are counted by the transport they arrived on, not by their User-Agent. The
raw header is never stored — only the family it mapped to.
"""

from __future__ import annotations

from collections.abc import Mapping

FAMILY_OPENCLAW_PLUGIN = "openclaw-plugin"
FAMILY_CLIENT_PYTHON = "caura-client-python"
FAMILY_CLIENT_NODE = "caura-client-node"
FAMILY_RAIL_PYTHON = "caura-rail-python"
FAMILY_RAIL_NODE = "caura-rail-node"
FAMILY_MCP = "mcp"
FAMILY_OTHER = "other"

# Fixed key set — also the ``clients_24h`` keys in the schema. Order is the
# order they appear in the payload.
FAMILIES: tuple[str, ...] = (
    FAMILY_OPENCLAW_PLUGIN,
    FAMILY_CLIENT_PYTHON,
    FAMILY_CLIENT_NODE,
    FAMILY_RAIL_PYTHON,
    FAMILY_RAIL_NODE,
    FAMILY_MCP,
    FAMILY_OTHER,
)

# Prefix → family. Each family's prefix is its own name, so a
# ``User-Agent: caura-rail-python/1.0.1 (py3.12)`` maps by ``startswith``.
_PREFIXES: tuple[tuple[str, str], ...] = tuple(
    (family, family) for family in FAMILIES if family not in (FAMILY_MCP, FAMILY_OTHER)
)

_enabled = False
_counts: dict[str, int] = dict.fromkeys(FAMILIES, 0)


def enable() -> None:
    """Start counting. Called once by the sender when the policy says on."""
    global _enabled
    _enabled = True


def disable() -> None:
    """Stop counting and drop the current counts (tests, shutdown)."""
    global _enabled
    _enabled = False
    reset()


def is_enabled() -> bool:
    return _enabled


def family_for(user_agent: str | None) -> str:
    """Map a ``User-Agent`` header to a family; unknown or absent → ``other``."""
    if not user_agent:
        return FAMILY_OTHER
    ua = user_agent.strip().lower()
    for prefix, family in _PREFIXES:
        if ua.startswith(prefix):
            return family
    return FAMILY_OTHER


def record(user_agent: str | None) -> None:
    """Count one authenticated REST request by its User-Agent family."""
    if not _enabled:
        return
    _counts[family_for(user_agent)] += 1


def record_mcp() -> None:
    """Count one authenticated MCP request."""
    if not _enabled:
        return
    _counts[FAMILY_MCP] += 1


def snapshot() -> dict[str, int]:
    """Copy of the current counts with every family present."""
    return {family: _counts.get(family, 0) for family in FAMILIES}


def reset() -> None:
    """Zero every family. The sender calls this after a successful send only."""
    for family in FAMILIES:
        _counts[family] = 0


def subtract(counts: Mapping[str, int]) -> None:
    """Drop counts that another worker already reported (see ``state.flush``).

    Clamped at zero: a subtraction can never leave a negative count behind.
    """
    for family in FAMILIES:
        _counts[family] = max(0, _counts[family] - int(counts.get(family, 0)))
