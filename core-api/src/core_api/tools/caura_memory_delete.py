"""ToolSpec for caura_memory_delete — soft-delete memories.

Directory surface only (``_types.Surface``): a narrowing of ``caura_manage``
whose handler forwards to ``caura_manage``'s, so every gate is that tool's.
"""

from core_api import mcp_server

from ._builders import mcp_register
from ._registry import register
from ._types import DIRECTORY_SURFACE, ToolSpec
from .caura_manage import _SPEC as _DELEGATE

_OPS = frozenset({"delete", "bulk_delete"})

_DESCRIPTION = (
    "Soft-delete one memory (delete) or up to 1000 by id (bulk_delete); an agent needs trust 3. "
    "To retire a memory that is merely out of date, prefer caura_memory_update op=transition status=outdated."
)

_SPEC = ToolSpec(
    name="caura_memory_delete",
    title="Delete memories",
    description=_DESCRIPTION,
    handler=mcp_server.caura_memory_delete,
    plugin_exposed=False,
    trust_required=0,
    surfaces=(DIRECTORY_SURFACE,),
    delegate="caura_manage",
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
