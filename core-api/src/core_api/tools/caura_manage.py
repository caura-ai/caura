"""ToolSpec for caura_manage — per-memory lifecycle (op-dispatched).

Ops: read (by id), update, transition, delete, bulk_delete, lineage, verify,
unverify, send_correction, confirm_correction — the same ten the handler accepts.
"""

from core_api import mcp_server

from ._builders import mcp_register
from ._registry import register
from ._types import OpSpec, ToolSpec

# Served to BOTH surfaces: MCP reads it from the registry, and the plugin gets
# the same text via /tool-descriptions. The plugin has no dispatch for
# bulk_delete or lineage, so naming that gap is load-bearing — and the two
# clauses about them travel together or not at all, since naming lineage
# without the caveat advertises it to the one surface that cannot serve it. It
# is the PLUGIN's gap, not MCP's: REST serves both (POST /memories/bulk-delete,
# GET /memories/{id}/contradictions), so "MCP-only" was wrong (L-137).
#
# Every token here is charged to every session's tools/list
# (tests/test_mcp_token_budget.py records the arithmetic), so say only what the
# inputSchema does not already carry.
_DESCRIPTION = (
    "Per-memory lifecycle. bulk_delete, lineage, verify, unverify and the *_correction ops are not in the plugin; "
    "lineage returns the supersession chain. "
    "update patches fields and re-embeds if content changes. "
    "transition sets status (active|pending|confirmed|cancelled|outdated|conflicted|archived|deleted). "
    "delete and bulk_delete are soft (prefer transition to outdated). "
    "verify sets human_verified (trust 3), unverify clears it. "
    "send_correction notifies the agents that read a corrected memory (trust 3); "
    "confirm_correction acks a notice sent to you."
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
    # reaches ``enforce_update`` via ``update_memory``, or ``enforce_transition``
    # when it sets a new status; ``transition`` calls
    # ``enforce_transition``, which is ``enforce_update`` plus ``enforce_delete``
    # for a status outside the live set (bar an agent retiring its own row), so
    # its bar depends on the row and the status asked for; and ``authorize_memory_access`` consults ``trust_level`` (>= 2 read,
    # >= 3 write) on cross-fleet rows. Nothing corroborates per-op attribution —
    # invariant 3 asks only whether a tool declaring a threshold reaches SOME
    # trust gate — so this claims no more than the two ``enforce_delete``
    # calls and the ``enforce_verify`` call visible in the handler.
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
        # EC-14. ``enforce_verify`` is the gate (>= 3, agent credentials only).
        OpSpec(
            name="verify",
            description="Mark a memory human_verified.",
            required_params=("memory_id",),
            trust_required=3,
        ),
        OpSpec(
            name="unverify",
            description="Withdraw a memory's verification.",
            required_params=("memory_id",),
            trust_required=3,
        ),
        # CP-11. ``enforce_send_correction`` is the gate (>= 3, agent credentials only).
        OpSpec(
            name="send_correction",
            description="Send a correction's notices to the agents that read the old version.",
            required_params=("correction_id",),
            trust_required=3,
        ),
        OpSpec(
            name="confirm_correction",
            description="Confirm a correction notice sent to you.",
            required_params=("correction_id",),
        ),
    ),
    error_codes=(
        "AGENT_CREDENTIAL_FORBIDDEN",
        "AGENT_IDENTITY_MISMATCH",
        "AGENT_NOT_REGISTERED",
        "AGENT_TRUST_TOO_LOW",
        "BAD_REQUEST",
        "CREDENTIAL_KIND_FORBIDDEN",
        "DUPLICATE_MEMORY",
        "FLEET_SCOPE_FORBIDDEN",
        "FORBIDDEN",
        "INVALID_ARGUMENTS",
        "KEPT_PRIVATE_BY_POLICY",
        "MISSING_AGENT_ID",
        "NOT_FOUND",
        "ORG_ADMIN_REQUIRED",
        "PERMISSION_DENIED",
        "PLAN_LIMIT_READ_ONLY",
        "REQUEST_BUDGET_EXCEEDED",
        "UNAUTHORIZED",
        "UNAVAILABLE",
    ),
)
register(_SPEC)
mcp_register(mcp_server.mcp, _SPEC)
