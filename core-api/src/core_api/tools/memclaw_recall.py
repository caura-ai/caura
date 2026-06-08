"""ToolSpec for memclaw_recall — hybrid semantic+keyword search.

Set ``include_brief=true`` to get an LLM-synthesized summary alongside
raw results.
"""

from core_api import mcp_server

from ._builders import mcp_register
from ._registry import register
from ._types import ToolSpec

_DESCRIPTION = (
    "Find memories by meaning (hybrid semantic+keyword). "
    "Set include_brief=true for an LLM summary paragraph. "
    "For non-semantic browse use memclaw_list; for read-by-id use memclaw_manage op=read."
)

_SPEC = ToolSpec(
    name="memclaw_recall",
    description=_DESCRIPTION,
    handler=mcp_server.memclaw_recall,
    plugin_exposed=True,
    trust_required=0,
)
register(_SPEC)
mcp_register(mcp_server.mcp, _SPEC)
