"""ToolSpec for caura_manage — per-memory lifecycle (op-dispatched).

Ops: read (by id), update, transition, delete, bulk_delete, lineage — the
same six the handler accepts.
"""

from core_api import mcp_server

from ._builders import mcp_register
from ._registry import register
from ._types import OpSpec, ToolSpec

# Served to BOTH surfaces: MCP reads it from the registry, and the plugin gets
# the same text via /tool-descriptions. The plugin dispatches over REST and has
# no endpoint for bulk_delete or lineage, so naming them MCP-only is
# load-bearing — and the two clauses about them travel together or not at all,
# since naming lineage without the caveat advertises it to the one surface that
# cannot serve it.
#
# Every token here is charged to every session's tools/list
# (tests/test_mcp_token_budget.py records the arithmetic), so say only what the
# inputSchema does not already carry.
_DESCRIPTION = (
    "Per-memory lifecycle. bulk_delete and lineage are MCP-only; lineage returns "
    "the supersession chain. "
    "update patches fields and re-embeds if content changes. "
    "transition sets status (active|pending|confirmed|cancelled|outdated|conflicted|archived|deleted). "
    "delete and bulk_delete are soft (prefer transition to outdated/archived)."
)

_SPEC = ToolSpec(
    name="caura_manage",
    description=_DESCRIPTION,
    handler=mcp_server.caura_manage,
    plugin_exposed=True,
    trust_required=0,
    # Every op the handler's ``_valid_ops`` accepts, in that order;
    # ``tests/test_mcp_authz_gate_inventory`` invariant 1 compares the two sets.
    #
    # ``trust_required`` is the ladder position each op's OWN branch asserts —
    # ``delete`` and ``bulk_delete`` call ``enforce_delete`` (>= 3) — and NOT
    # the effective threshold. Do not read the zeros as "ungated": ``update``
    # reaches ``enforce_update`` via ``update_memory``, and
    # ``authorize_memory_access`` consults ``trust_level`` (>= 2 read, >= 3
    # write) on cross-fleet rows. Nothing corroborates per-op attribution —
    # invariant 3 asks only whether a tool declaring a threshold reaches SOME
    # trust gate — so this claims no more than the two ``enforce_delete``
    # calls visible in the handler.
    ops=(
        OpSpec(name="read", description="Fetch a memory by id.", required_params=("memory_id",)),
        OpSpec(name="update", description="Patch memory fields.", required_params=("memory_id",)),
        OpSpec(
            name="transition",
            description="Set lifecycle status.",
            required_params=("memory_id", "status"),
        ),
        OpSpec(
            name="delete",
            description="Soft-delete a memory.",
            required_params=("memory_id",),
            trust_required=3,
        ),
        OpSpec(
            name="bulk_delete",
            description="Soft-delete up to 1000 memories by id.",
            required_params=("memory_ids",),
            trust_required=3,
        ),
        OpSpec(
            name="lineage",
            description="Walk the supersession chain for a memory.",
            required_params=("memory_id",),
        ),
    ),
    error_codes=(
        "FORBIDDEN",
        "INVALID_ARGUMENTS",
        "MISSING_AGENT_ID",
        "NOT_FOUND",
        "PERMISSION_DENIED",
        "UNAUTHORIZED",
    ),
)
register(_SPEC)
mcp_register(mcp_server.mcp, _SPEC)
