"""ToolSpec for memclaw_insights — Karpathy Loop reflection step.

LLM-driven analysis over targeted memory subsets. Findings are persisted
as ``insight``-typed memories so they compound across sessions.

scope='agent' at trust ≥ 1; scope='fleet'/'all' requires trust ≥ 2.
Exposed via both MCP and REST (``/api/v1/insights/generate``).
"""

from core_api import mcp_server

from ._builders import mcp_register
from ._registry import register
from ._types import ToolSpec

_DESCRIPTION = (
    "Reflect over your memory store. focus: contradictions|failures|stale|divergence|"
    "patterns|discover. scope: agent (default, trust ≥ 1)|fleet|all (trust ≥ 2; "
    "divergence requires fleet/all). "
    "Findings are saved as insight-type memories for future runs to build on."
)

_SPEC = ToolSpec(
    name="memclaw_insights",
    description=_DESCRIPTION,
    handler=mcp_server.memclaw_insights,
    plugin_exposed=True,
    trust_required=1,
    impl_status="live",
)
register(_SPEC)
mcp_register(mcp_server.mcp, _SPEC)
