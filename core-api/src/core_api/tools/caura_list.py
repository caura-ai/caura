"""ToolSpec for caura_list — non-semantic memory enumeration.

Filter, sort, and paginate memories by metadata. NOT semantic search
(use ``caura_recall``). scope='agent' at trust ≥ 1; scope='fleet' reads
your OWN fleet at trust ≥ 1, a different fleet at trust ≥ 2; scope='all'
(tenant-wide) requires trust ≥ 2. Trust 3 unlocks ``include_deleted``.
"""

from core_api import mcp_server

from ._builders import mcp_register
from ._registry import register
from ._types import ToolSpec

# ``scope`` has no single default: this one string is served to BOTH the
# MCP surface and the plugin (see ``_types.ToolSpec`` — it feeds
# ``/tool-descriptions`` and the generated ``plugin/tools.json``), and only
# MCP defaults it. ``mcp_server.caura_list`` declares ``scope=... = "agent"``,
# while ``GET /memories`` declares it ``Query(default=None)`` and consults the
# trust ladder only when the caller supplied one — an omitted ``scope`` there
# takes its author filter from ``written_by ?? agent_id`` and skips
# ``require_trust`` entirely. So the description states the ladder per VALUE
# and names the per-surface default separately, instead of calling 'agent'
# "the default" on a surface where omitting it is a different request.
_DESCRIPTION = (
    "Browse memories by metadata (non-semantic). Filter+sort+paginate by fleet, author, type, "
    "status, weight, created-at. scope='agent' lists your memories at trust ≥ 1; "
    "scope='fleet' reads your OWN fleet at trust ≥ 1, a different fleet at trust ≥ 2; "
    "scope='all' (tenant-wide) requires trust ≥ 2. Omitted scope means 'agent' on MCP; "
    "over REST/plugin it filters by agent_id with no trust gate. "
    "Trust 3 unlocks include_deleted. "
    "Cursor pagination requires sort=created_at order=desc. For semantic search use caura_recall."
)

_SPEC = ToolSpec(
    name="caura_list",
    description=_DESCRIPTION,
    handler=mcp_server.caura_list,
    plugin_exposed=True,
    trust_required=1,
)
register(_SPEC)
mcp_register(mcp_server.mcp, _SPEC)
