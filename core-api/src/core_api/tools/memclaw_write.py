"""ToolSpec for memclaw_write — single OR batch.

Provide exactly one of {content, items}. The system auto-classifies type,
weight, title, summary, tags, and temporal dates.
"""

from core_api import mcp_server

from ._builders import mcp_register
from ._registry import register
from ._types import ToolSpec

_DESCRIPTION = (
    "Store NEW memories. Provide exactly one of {content, items} (batch ≤100). "
    "System auto-classifies type, importance, title, tags, dates. "
    "visibility: scope_team (default) / scope_org / scope_agent. Prefer team/org for sharing."
)

_SPEC = ToolSpec(
    name="memclaw_write",
    description=_DESCRIPTION,
    handler=mcp_server.memclaw_write,
    plugin_exposed=True,
    trust_required=0,
    error_codes=("INVALID_ARGUMENTS", "BATCH_TOO_LARGE", "INVALID_BATCH_ITEM"),
)
register(_SPEC)
mcp_register(mcp_server.mcp, _SPEC)
