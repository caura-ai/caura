"""ToolSpec for memclaw_entity_get — entity lookup by UUID."""

from core_api import mcp_server

from ._builders import mcp_register
from ._registry import register
from ._types import ToolSpec

_DESCRIPTION = "Look up an entity by UUID. Use only when you have an entity_id from a prior search."

_SPEC = ToolSpec(
    name="memclaw_entity_get",
    description=_DESCRIPTION,
    handler=mcp_server.memclaw_entity_get,
    plugin_exposed=True,
    trust_required=0,
)
register(_SPEC)
mcp_register(mcp_server.mcp, _SPEC)
