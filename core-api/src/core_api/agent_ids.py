"""Shared agent-id constants.

A tiny, dependency-free module so both the MCP server (``mcp_server``) and the
REST routes (``routes.memories``) can reference the reserved default identity
without importing each other — avoids the heavy/cross-private import that would
otherwise couple the route module to the whole MCP tool surface.
"""

# Reserved fallback identity used when a caller omits ``agent_id`` on the
# single-tenant / standalone path. On the enterprise gateway this value is
# explicitly refused (see ``mcp_server._refuse_default_agent_on_gateway``) so
# anonymous writes are never silently attributed to one shared identity.
DEFAULT_AGENT_ID = "mcp-agent"

# Bare ``"main"`` is the OpenClaw plugin's *unset* default agent_id: when an
# operator never sets ``MEMCLAW_AGENT_ID`` every install collapses onto this one
# shared identity (the eToro "firehose"). Unlike ``DEFAULT_AGENT_ID``
# ("mcp-agent", legitimate on the standalone path), bare ``"main"`` is never a
# valid *persisted* write identity and is refused on every write path.
RESERVED_MAIN_AGENT_ID = "main"

# Ids rejected on EVERY write path regardless of context (never legitimate,
# including standalone), enforced by the write-path guard
# (``services.agent_identity``). ``DEFAULT_AGENT_ID`` ("mcp-agent") is
# intentionally excluded — standalone uses it and it is guarded *conditionally*
# by ``mcp_server._refuse_default_agent_on_gateway`` instead.
ALWAYS_RESERVED_AGENT_IDS = frozenset({RESERVED_MAIN_AGENT_ID})

# Ids that must NOT be accepted as a *body-supplied* (client self-declared)
# write identity: the unset plugin default ("main") and the MCP tool-param
# default ("mcp-agent", which means "caller specified nothing"). Gates the BODY
# only — see ``effective_write_agent_id``. The VERIFIED id is checked against
# ``ALWAYS_RESERVED_AGENT_IDS`` ({main}) so a real (if poorly-named)
# ``home_agent_id="mcp-agent"`` credential still wins and cannot be overridden
# by the body (closing the spoof vector that treating verified "mcp-agent" as a
# placeholder would open).
_PLACEHOLDER_BODY_AGENT_IDS = frozenset({RESERVED_MAIN_AGENT_ID, DEFAULT_AGENT_ID})


def effective_write_agent_id(verified_id: str | None, body_id: str | None) -> str | None:
    """Resolve the agent_id a WRITE is attributed to from the verified
    credential identity (gateway ``X-Agent-ID`` = ``home_agent_id``) and the
    client-supplied body id.

    Rule: the verified id wins — **unless** it is the reserved ``main``
    placeholder (a misconfigured ``home_agent_id="main"`` cred), in which case a
    *non-default* body id is honored so the install can self-identify instead of
    collapsing onto "main". A verified ``mcp-agent`` home is a real (if
    poorly-named) identity and still wins — it is NOT overridable by the body. If
    no real identity is available, the reserved id falls through and the
    write-path guard (policy=reject) refuses it.

    Asymmetric on purpose: the *verified* check uses ``ALWAYS_RESERVED_AGENT_IDS``
    ({main}) so a real verified id (incl. "mcp-agent") can't be body-spoofed; the
    *body* check uses ``_PLACEHOLDER_BODY_AGENT_IDS`` ({main, mcp-agent}) so a
    defaulted body can't become the identity. Strictly *tighter* than the REST
    write path, which already trusts the body unconditionally.
    """
    if verified_id and verified_id not in ALWAYS_RESERVED_AGENT_IDS:
        return verified_id
    if body_id and body_id not in _PLACEHOLDER_BODY_AGENT_IDS:
        return body_id
    return verified_id or body_id
