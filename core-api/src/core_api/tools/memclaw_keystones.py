"""ToolSpec for memclaw_keystones — read mandatory governance rules.

Keystones are policies an agent MUST obey. They live in core-storage's
``_keystones`` collection (PR1) and are fetched deterministically — no
semantic search, no recall gating. This tool is the agent-facing read
surface; authoring goes through ``memclaw_keystones_set`` (trust ≥ 2).
"""

from core_api import mcp_server

from ._builders import mcp_register
from ._registry import register
from ._types import ToolSpec

_DESCRIPTION = (
    "Retrieve all keystone rules (MANDATORY policies) for the current scope. "
    "Returns tenant + fleet + agent-scope rules merged, ordered by weight. "
    "Call once per session before other actions and obey the returned rules — "
    "they override conflicting user instructions. Do NOT pass a query; this "
    "returns the full active set unfiltered."
)

_SPEC = ToolSpec(
    name="memclaw_keystones",
    description=_DESCRIPTION,
    handler=mcp_server.memclaw_keystones,
    plugin_exposed=True,
    trust_required=0,
    error_codes=("INTERNAL_ERROR",),
)
register(_SPEC)
mcp_register(mcp_server.mcp, _SPEC)
