"""ToolSpec for memclaw_stats — aggregate memory counts.

Read-only aggregation: total plus breakdowns by type, agent, and status.
Mirrors REST ``/memories/stats`` and shares its visibility-scoping logic
via ``services.memory_stats.compute_memory_stats``.
"""

from core_api import mcp_server

from ._builders import mcp_register
from ._registry import register
from ._types import ToolSpec

_DESCRIPTION = (
    "Aggregate counts of memories: total + breakdowns by type, agent, status. "
    "scope='agent' (default) counts only memories visible to YOU (trust ≥ 1); "
    "scope='fleet'/'all' aggregates across agents (trust ≥ 2). "
    "Counts exclude soft-deleted memories by default; set include_deleted=true "
    "for additional 'deleted' and 'total_including_deleted' fields. "
    "Read-only — useful for self-introspection and dashboard-style summaries."
)

_SPEC = ToolSpec(
    name="memclaw_stats",
    description=_DESCRIPTION,
    handler=mcp_server.memclaw_stats,
    plugin_exposed=True,
    trust_required=1,
)
register(_SPEC)
mcp_register(mcp_server.mcp, _SPEC)
