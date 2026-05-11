"""ToolSpec for memclaw_keystones_set — author/remove governance rules.

Trust ≥ 1 required: keystones override user instructions, so a
prompt-injected low-trust agent must not be able to plant one. Reads
go through ``memclaw_keystones`` (open).

Op-dispatched in one tool (set|delete) rather than two named tools so
the write surface lives in a single, clearly admin-flavoured place.
"""

from core_api import mcp_server

from ._builders import mcp_register
from ._registry import register
from ._types import OpSpec, ToolSpec

_DESCRIPTION = (
    "Author or remove keystone rules. op: set|delete. "
    "set requires {doc_id, title, content, scope, weight}; "
    "scope ∈ {tenant, fleet, agent}; weight ∈ {low, med, high}. "
    "scope=fleet|agent requires fleet_id; scope=agent additionally requires agent_id. "
    "delete requires {doc_id}. "
    "Requires trust ≥ 1 — keystones override user instructions, so a "
    "compromised low-trust agent must not be able to plant one."
)

_SPEC = ToolSpec(
    name="memclaw_keystones_set",
    description=_DESCRIPTION,
    handler=mcp_server.memclaw_keystones_set,
    plugin_exposed=False,
    trust_required=1,
    ops=(
        OpSpec(
            name="set",
            description="Upsert a keystone rule by doc_id.",
            required_params=("doc_id", "title", "content", "scope", "weight"),
        ),
        OpSpec(
            name="delete",
            description="Remove a keystone rule by doc_id.",
            required_params=("doc_id",),
        ),
    ),
    error_codes=("INVALID_ARGUMENTS", "FORBIDDEN", "NOT_FOUND"),
)
register(_SPEC)
mcp_register(mcp_server.mcp, _SPEC)
