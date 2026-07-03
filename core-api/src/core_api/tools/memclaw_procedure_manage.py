"""ToolSpec for memclaw_procedure_manage — manual procedure lifecycle (BP-02).

The runtime loop (memclaw_procedure_record) auto-quarantines unreliable
procedures; this op-dispatched tool is the manual override + curation
surface Brain exposes as delete/invalidate/quarantine/unquarantine +
get_procedure_stats. Mirrors the memclaw_manage shape.
"""

from core_api import mcp_server

from ._builders import mcp_register
from ._registry import register
from ._types import OpSpec, ToolSpec

_DESCRIPTION = (
    "Manual procedure lifecycle. op: stats|quarantine|unquarantine|invalidate|delete. "
    "stats reads reliability telemetry (success/failure counts, reliability_score, "
    "is_quarantined). quarantine/unquarantine flip the reversible quarantine flag "
    "(manual override of the auto-quarantine the record path applies); invalidate "
    "permanently retires a procedure (status='invalidated'); delete hard-removes it. "
    "Agents can only manage procedures in their own tenant."
)

_SPEC = ToolSpec(
    name="memclaw_procedure_manage",
    description=_DESCRIPTION,
    handler=mcp_server.memclaw_procedure_manage,
    plugin_exposed=True,
    trust_required=0,
    ops=(
        OpSpec(
            name="stats",
            description="Read a procedure's reliability stats.",
            required_params=("procedure_id",),
        ),
        OpSpec(
            name="quarantine",
            description="Suspend a procedure from the ranker (reversible).",
            required_params=("procedure_id",),
            trust_required=2,
        ),
        OpSpec(
            name="unquarantine",
            description="Restore a quarantined procedure to the ranker.",
            required_params=("procedure_id",),
            trust_required=2,
        ),
        OpSpec(
            name="invalidate",
            description="Permanently retire a procedure (status='invalidated').",
            required_params=("procedure_id",),
            trust_required=2,
        ),
        OpSpec(
            name="delete",
            description="Hard-delete a procedure and its stats.",
            required_params=("procedure_id",),
            trust_required=3,
        ),
    ),
    error_codes=("INVALID_ARGUMENTS", "FORBIDDEN", "NOT_FOUND"),
)
register(_SPEC)
mcp_register(mcp_server.mcp, _SPEC)
