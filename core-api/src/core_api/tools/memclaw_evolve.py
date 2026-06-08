"""ToolSpec for memclaw_evolve — Karpathy Loop feedback edge.

Records a real-world outcome against the memories that influenced an
action: adjusts weights, optionally generates preventive rules on
failure/partial outcomes.

scope='agent' (default) at trust ≥ 1; scope='fleet'/'all' requires trust ≥ 2.
Exposed via both MCP and REST (``/api/v1/evolve/report``).
"""

from core_api import mcp_server

from ._builders import mcp_register
from ._registry import register
from ._types import ToolSpec

_DESCRIPTION = (
    "Report what happened after acting on memories. outcome_type: success|failure|partial. "
    "related_ids = the memory UUIDs that influenced the action (use IDs from your most recent "
    "memclaw_recall). scope: agent (default, trust ≥ 1)|fleet|all (trust ≥ 2; fleet_id required "
    "when scope='fleet'). Weight adjustments and rule generation are scoped — agents can only "
    "evolve memories they own (scope=agent), their fleet (scope=fleet), or any (scope=all)."
)

_SPEC = ToolSpec(
    name="memclaw_evolve",
    description=_DESCRIPTION,
    handler=mcp_server.memclaw_evolve,
    plugin_exposed=True,
    trust_required=1,
    impl_status="live",
)
register(_SPEC)
mcp_register(mcp_server.mcp, _SPEC)
