"""ToolSpec for memclaw_keystones_set — author/remove governance rules.

Trust gating is tiered per the target rule's scope:

* ``scope=agent`` AND ``agent_id == caller``: trust ≥ 1 (self-author).
* ``scope=fleet`` / ``scope=tenant`` / cross-agent ``scope=agent``:
  trust ≥ 2 (the cross-agent governance bar used elsewhere for
  ``memclaw_list/stats/evolve/insights`` with ``scope=fleet|all``).

Declared ``trust_required=1`` is the minimum any successful call needs;
the per-op floor is computed dynamically and enforced server-side
(mirrors the ``memclaw_evolve`` / ``memclaw_list`` pattern of dynamic
trust). Reads go through ``memclaw_keystones`` (open).

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
    "scope=fleet|agent requires fleet_id; scope=agent additionally requires "
    "agent_id — and ``agent_id`` here names the TARGET agent the rule binds "
    "to, NOT the caller (caller identity comes from the API key / gateway "
    "headers). For scope=tenant and scope=fleet you must OMIT ``agent_id``; "
    "passing it returns INVALID_ARGUMENTS. "
    "delete requires {doc_id}. "
    "Trust gating is dynamic: scope=agent where the TARGET agent_id matches "
    "the caller is trust ≥ 1 (self-author); anything else (scope=fleet, "
    "scope=tenant, or scope=agent targeting a different agent) is trust ≥ 2."
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
    error_codes=("INVALID_ARGUMENTS", "FORBIDDEN", "NOT_FOUND", "INTERNAL_ERROR"),
)
register(_SPEC)
mcp_register(mcp_server.mcp, _SPEC)
