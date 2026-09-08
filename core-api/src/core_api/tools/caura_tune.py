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

_SPEC = ToolSpec(
    name="caura_tune",
    description=_DESCRIPTION,
    handler=mcp_server.caura_tune,
    plugin_exposed=True,
    trust_required=0,
    error_codes=(
        "FORBIDDEN",
        "INVALID_ARGUMENTS",
        "MISSING_AGENT_ID",
        "UNAUTHORIZED",
    ),
)
register(_SPEC)
mcp_register(mcp_server.mcp, _SPEC)
