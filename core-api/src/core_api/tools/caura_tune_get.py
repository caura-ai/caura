"""ToolSpec for caura_tune_get — read the agent's retrieval defaults (read-only).

Directory surface only (``_types.Surface``): ``caura_tune`` reads when called
with no fields, which the directory cannot list as one tool, so there
``caura_tune`` is write-only and this serves the read. Its handler forwards to
``caura_tune``'s with no fields, so the read path and its gates are that tool's.
"""

from core_api import mcp_server

from ._builders import mcp_register
from ._registry import register
from ._types import DIRECTORY_SURFACE, ToolSpec
from .caura_tune import _SPEC as _DELEGATE

_DESCRIPTION = (
    "Return YOUR standing retrieval defaults for caura_recall (top_k, min_similarity, "
    "fts_weight, graph_max_hops, freshness, recall_boost). Changes nothing; "
    "use caura_tune to change them."
)

_SPEC = ToolSpec(
    name="caura_tune_get",
    title="Get recall defaults",
    description=_DESCRIPTION,
    handler=mcp_server.caura_tune_get,
    plugin_exposed=False,
    trust_required=0,
    read_only=True,
    surfaces=(DIRECTORY_SURFACE,),
    delegate="caura_tune",
    # The delegate's codes, plus the refusal for calling this tool on the
    # default surface.
    error_codes=tuple(sorted({*_DELEGATE.error_codes, "TOOL_NOT_ON_SURFACE"})),
)
register(_SPEC)
mcp_register(mcp_server.mcp, _SPEC)
