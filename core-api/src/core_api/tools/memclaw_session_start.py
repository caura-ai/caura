"""ToolSpec for memclaw_session_start — warm context injection at session start (UX-03)."""

from core_api import mcp_server

from ._builders import mcp_register
from ._registry import register
from ._types import ToolSpec

_DESCRIPTION = (
    "Call once at session start. Paste result into system prompt for zero-latency context. "
    "Returns the top-5 memories (recency/usage-blended re-rank, rule-typed rows excluded), "
    "active keystone rules, and procedures with reliability >= 0.6. "
    "Keys: memories (list), keystones (list), procedures (list)."
)

_SPEC = ToolSpec(
    name="memclaw_session_start",
    description=_DESCRIPTION,
    handler=mcp_server.memclaw_session_start,
    plugin_exposed=True,
    trust_required=0,
    error_codes=("INTERNAL_ERROR",),
)
register(_SPEC)
mcp_register(mcp_server.mcp, _SPEC)
