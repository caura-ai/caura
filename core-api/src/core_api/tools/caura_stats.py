"""ToolSpec for caura_stats — aggregate memory counts.

Read-only aggregation: total plus breakdowns by type, agent, and status.
Mirrors REST ``/memories/stats`` and shares its visibility-scoping logic
via ``services.memory_stats.compute_memory_stats``.
"""

from core_api import mcp_server

from ._builders import mcp_register
from ._registry import register
from ._types import ToolSpec

# ``scope`` has no single default — see the note in ``caura_list.py``. Same
# split here: ``mcp_server.caura_stats`` defaults it to 'agent', while
# ``GET /memories/stats`` declares it ``Query(default=None)`` and an omitted
# one aggregates over the ``agent_id`` query param without the trust gate.
_DESCRIPTION = (
    "Aggregate counts of memories: total + breakdowns by type, agent, status. "
    "scope='agent' counts only memories visible to YOU (trust ≥ 1); "
    "scope='fleet' aggregates your OWN fleet at trust ≥ 1, a different fleet at trust ≥ 2; "
    "scope='all' (tenant-wide) requires trust ≥ 2. Omitted scope means 'agent' on MCP; "
    "over REST/plugin it aggregates over agent_id with no trust gate. "
    "Counts exclude soft-deleted memories by default; set include_deleted=true "
    "for additional 'deleted' and 'total_including_deleted' fields. "
    "Read-only — useful for self-introspection and dashboard-style summaries."
)

_SPEC = ToolSpec(
    name="caura_stats",
    description=_DESCRIPTION,
    handler=mcp_server.caura_stats,
    plugin_exposed=True,
    trust_required=1,
)
register(_SPEC)
mcp_register(mcp_server.mcp, _SPEC)
