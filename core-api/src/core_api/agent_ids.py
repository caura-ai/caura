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
