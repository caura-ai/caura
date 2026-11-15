"""ToolSpec for caura_doc_delete — delete a structured document.

Directory surface only (``_types.Surface``): a narrowing of ``caura_doc``
whose handler forwards to ``caura_doc``'s, so every gate is that tool's.
"""

from core_api import mcp_server

from ._builders import mcp_register
from ._registry import register
from ._types import DIRECTORY_SURFACE, ToolSpec
from .caura_doc import _SPEC as _DELEGATE

_OPS = frozenset({"delete"})

_DESCRIPTION = "Delete a structured document by collection+doc_id. Also removes the memory its write minted."

_SPEC = ToolSpec(
    name="caura_doc_delete",
    title="Delete document",
    description=_DESCRIPTION,
    handler=mcp_server.caura_doc_delete,
    plugin_exposed=False,
    trust_required=0,
    surfaces=(DIRECTORY_SURFACE,),
    delegate="caura_doc",
    # The delegate's own OpSpecs for these ops, so published thresholds and
    # required params cannot drift between the two names for one op.
    ops=tuple(op for op in _DELEGATE.ops if op.name in _OPS),
    # The delegate's codes, plus the refusal for calling this tool on the
    # default surface. Over-declares the codes only other ops can reach, the
    # same trade the delegate makes across its own ops — except RATE_LIMITED,
    # which ``_admission`` charges caura_doc only for op=write|search.
    error_codes=tuple(sorted({*_DELEGATE.error_codes, "TOOL_NOT_ON_SURFACE"} - {"RATE_LIMITED"})),
)
register(_SPEC)
mcp_register(mcp_server.mcp, _SPEC)
