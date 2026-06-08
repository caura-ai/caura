"""ToolSpec for memclaw_list — non-semantic memory enumeration.

Filter, sort, and paginate memories by metadata. NOT semantic search
(use ``memclaw_recall``). scope='agent' at trust ≥ 1; scope='fleet'/'all'
requires trust ≥ 2. Trust 3 unlocks ``include_deleted``.
"""

from core_api import mcp_server

from ._builders import mcp_register
from ._registry import register
from ._types import ToolSpec

_DESCRIPTION = (
    "Browse memories by metadata (non-semantic). Filter+sort+paginate by fleet, author, type, "
    "status, weight, created-at. scope='agent' (default) lists your memories at trust ≥ 1; "
    "scope='fleet'/'all' requires trust ≥ 2. Trust 3 unlocks include_deleted. "
    "Cursor pagination requires sort=created_at order=desc. For semantic search use memclaw_recall."
)

_SPEC = ToolSpec(
    name="memclaw_list",
    description=_DESCRIPTION,
    handler=mcp_server.memclaw_list,
    plugin_exposed=True,
    trust_required=1,
)
register(_SPEC)
mcp_register(mcp_server.mcp, _SPEC)
