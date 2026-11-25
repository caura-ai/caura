"""ToolSpec for caura_doc_write — upsert a structured document.

Directory surface only (``_types.Surface``): a narrowing of ``caura_doc``
whose handler forwards to ``caura_doc``'s, so every gate is that tool's.
"""

from core_api import mcp_server

from ._builders import mcp_register
from ._registry import register
from ._types import DIRECTORY_SURFACE, ToolSpec
from .caura_doc import _SPEC as _DELEGATE

_OPS = frozenset({"write"})

_DESCRIPTION = (
    "Create or overwrite a structured document by collection+doc_id. Include data['summary'] "
    "(1-3 dense, intent-focused sentences) to make it searchable; only the summary is embedded. "
    "The write also mints a memory carrying the document's data, so the body is caura_recall-able; "
    "skills and _-prefixed collections are exempt. For free-form knowledge use caura_write."
)

_SPEC = ToolSpec(
    name="caura_doc_write",
    title="Write document",
    description=_DESCRIPTION,
    handler=mcp_server.caura_doc_write,
    plugin_exposed=False,
    trust_required=0,
    surfaces=(DIRECTORY_SURFACE,),
    delegate="caura_doc",
    # The delegate's own OpSpecs for these ops, so published thresholds and
    # required params cannot drift between the two names for one op.
    ops=tuple(op for op in _DELEGATE.ops if op.name in _OPS),
    # The delegate's codes, plus the refusal for calling this tool on the
    # default surface. Over-declares the codes only other ops can reach, the
    # same trade the delegate makes across its own ops.
    error_codes=tuple(sorted({*_DELEGATE.error_codes, "TOOL_NOT_ON_SURFACE"})),
)
register(_SPEC)
mcp_register(mcp_server.mcp, _SPEC)
