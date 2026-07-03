"""Reserved-agent-id write guard (Phase 1 of the `main` identity fix).

Bare ``agent_id="main"`` is the OpenClaw plugin's unset default; without a
per-install ``MEMCLAW_AGENT_ID`` every install collapses onto one shared
identity (the eToro "firehose"). This module reserves it on the write path so
new writes can't keep refilling that bucket, and returns an actionable message
telling the agent how to set a real identity.

Enforced at the ``memory_service.create_memory`` / ``create_memories_bulk``
service boundary — the single funnel for REST, MCP, and STM writes — so no
entry point can bypass it. By the time those run, ``data.agent_id`` is the
resolved *effective* identity, so a write that supplies a unique ``agent_id``
(the escape hatch) always passes; only the reserved default is refused.

Reserved-id set lives in ``core_api.agent_ids`` (shared with the MCP gateway
guard). ``"mcp-agent"`` is deliberately NOT unconditionally reserved here —
standalone uses it and it has its own gateway-conditional guard
(``mcp_server._refuse_default_agent_on_gateway``).
"""

from __future__ import annotations

import logging

from core_api.agent_ids import ALWAYS_RESERVED_AGENT_IDS
from core_api.config import settings

logger = logging.getLogger(__name__)


class ReservedAgentIdError(Exception):
    """Raised when a write targets an unconditionally reserved agent_id.

    Domain exception — the service layer (``memory_service``) converts it to an
    HTTP 409 at the ``create_memory`` / ``create_memories_bulk`` boundary, so
    this guard stays framework-agnostic.
    """


RESERVED_WRITE_ID_MESSAGE = (
    'agent_id "{agent_id}" is reserved and no longer accepts writes — it is the '
    "unset plugin default and collides across every install. Retry with a "
    "unique, STABLE agent_id for THIS install: (1) set MEMCLAW_AGENT_ID in "
    '~/.openclaw/plugins/memclaw/.env to a stable name (e.g. "webclaw") and '
    "restart the plugin; (2) or pass a unique agent_id argument on each write "
    'call now; (3) if you have no name, use "main-<install_id>" with '
    "<install_id> from ~/.openclaw/plugins/memclaw/install.json. The id must "
    "not be reserved and must be identical on every run. Recall and existing "
    "memories are unaffected."
)


def reserved_write_refusal(agent_id: str | None) -> str | None:
    """Return a 409 detail string if ``agent_id`` is an unconditionally reserved
    write identity under the active policy; otherwise ``None`` (proceed).

    Escape hatch: any non-reserved id (including ``main-<install_id>``) returns
    ``None``. Policy (``settings.reserved_agent_id_policy``):
    ``allow`` → never refuse; ``warn`` → log + proceed (observe-only);
    ``reject`` → refuse.
    """
    if not agent_id or agent_id not in ALWAYS_RESERVED_AGENT_IDS:
        return None
    policy = settings.reserved_agent_id_policy
    if policy == "allow":
        return None
    # Structured signal so ops can count reserved writes and confirm the
    # bare-"main" write rate drops to ~0 before flipping warn -> reject and
    # before the firehose delete. core-api has no metrics backend, so the log
    # IS the counter (the same channel the firehose was originally sourced
    # from in the usage analysis).
    logger.warning(
        "reserved_agent_write",
        extra={"reserved_agent_id": agent_id, "policy": policy},
    )
    if policy == "warn":
        return None
    return RESERVED_WRITE_ID_MESSAGE.format(agent_id=agent_id)


def enforce_reserved_write_id(agent_id: str | None) -> None:
    """Raise ``ReservedAgentIdError`` when the resolved write identity is
    reserved under ``policy="reject"``. No-op under ``allow``/``warn``
    (``warn`` logs).

    The service layer converts this to an HTTP 409; REST surfaces it directly
    and the MCP write tool's ``except HTTPException`` maps ``.detail`` into its
    error envelope, so the agent sees the guidance.
    """
    if detail := reserved_write_refusal(agent_id):
        raise ReservedAgentIdError(detail)
