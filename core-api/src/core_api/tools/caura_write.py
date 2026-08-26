"""ToolSpec for caura_write — single OR batch.

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
    "visibility: scope_team (default) / scope_org / scope_agent. Prefer team/org for sharing. "
    "If the response has metadata.embedding_pending=true the memory is saved but not yet "
    "semantically searchable (~15-20s); it is already findable by keyword and in caura_list, "
    "so do not re-write it. Pass write_mode='strong' to embed inline when you must recall it now. "
    "Unknown keys are rejected, not ignored: put your own under metadata."
)

_SPEC = ToolSpec(
    name="caura_write",
    description=_DESCRIPTION,
    handler=mcp_server.caura_write,
    plugin_exposed=True,
    trust_required=0,
    error_codes=("INVALID_ARGUMENTS", "BATCH_TOO_LARGE", "INVALID_BATCH_ITEM"),
)
register(_SPEC)
mcp_register(mcp_server.mcp, _SPEC)
