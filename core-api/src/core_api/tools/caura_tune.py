"""ToolSpec for caura_tune — per-agent retrieval parameter tuning."""

from core_api import mcp_server

from ._builders import mcp_register
from ._registry import register
from ._types import ToolSpec

_DESCRIPTION = (
    "Persistently tune YOUR standing retrieval defaults for caura_recall (top_k, "
    "min_similarity, fts_weight, graph_max_hops, freshness, recall_boost). "
    "Only provide fields to change; no fields → returns current profile."
)

# On the directory surface caura_tune only writes: its read form (no fields)
# is refused there in favour of caura_tune_get (``ToolSpec.directory_read_via``),
# because the directory does not accept one tool that both reads and writes.
_DIRECTORY_DESCRIPTION = (
    "Persistently tune YOUR standing retrieval defaults for caura_recall (top_k, "
    "min_similarity, fts_weight, graph_max_hops, freshness, recall_boost). "
    "Provide the fields to change, or reset/reset_knobs to clear them; "
    "to read the current profile use caura_tune_get."
)

_SPEC = ToolSpec(
    name="caura_tune",
    title="Tune recall defaults",
    description=_DESCRIPTION,
    directory_description=_DIRECTORY_DESCRIPTION,
    directory_read_via="caura_tune_get",
    handler=mcp_server.caura_tune,
    plugin_exposed=True,
    trust_required=0,
    error_codes=(
        "FORBIDDEN",
        "INVALID_ARGUMENTS",
        "MISSING_AGENT_ID",
        "NOT_FOUND",
        "PLAN_LIMIT_READ_ONLY",
        "REQUEST_BUDGET_EXCEEDED",
        "TOOL_NOT_ON_SURFACE",
        "UNAUTHORIZED",
    ),
)
register(_SPEC)
mcp_register(mcp_server.mcp, _SPEC)
