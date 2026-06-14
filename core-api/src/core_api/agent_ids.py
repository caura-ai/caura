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
