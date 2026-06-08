"""ToolSpec for memclaw_manage — per-memory lifecycle (op-dispatched).

Ops: read (by id), update, transition, delete. Per-op required params
are documented in the description below.
"""

from core_api import mcp_server

from ._builders import mcp_register
from ._registry import register
from ._types import OpSpec, ToolSpec

_DESCRIPTION = (
    "Per-memory lifecycle. op: read|update|transition|delete. "
    "update patches fields and re-embeds if content changes. "
    "transition sets status (active|pending|confirmed|cancelled|outdated|conflicted|archived|deleted). "
    "delete is soft (prefer transition to outdated/archived)."
)

_SPEC = ToolSpec(
    name="memclaw_manage",
    description=_DESCRIPTION,
    handler=mcp_server.memclaw_manage,
    plugin_exposed=True,
    trust_required=0,
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
    ),
    error_codes=("INVALID_ARGUMENTS",),
)
register(_SPEC)
mcp_register(mcp_server.mcp, _SPEC)
