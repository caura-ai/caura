"""ToolSpec for caura_memory_update — change a memory, a correction notice or a baton.

Directory surface only (``_types.Surface``): a narrowing of ``caura_manage``
whose handler forwards to ``caura_manage``'s, so every gate is that tool's.
"""

from core_api import mcp_server

from ._builders import mcp_register
from ._registry import register
from ._types import DIRECTORY_SURFACE, ToolSpec
from .caura_manage import _SPEC as _DELEGATE

_OPS = frozenset(
    {"transition", "unverify", "verify", "confirm_correction", "accept_baton", "send_correction", "update"}
)

_DESCRIPTION = (
    "Change an existing memory or act on a notice. "
    "update patches fields and re-embeds if content changes. "
    "transition sets status (active|pending|confirmed|cancelled|outdated|conflicted|archived|deleted). "
    "verify sets human_verified (trust 3), unverify clears it. "
    "send_correction notifies the agents that read a corrected memory (trust 3); "
    "confirm_correction acks a notice sent to you. accept_baton takes up a handoff baton handed to you."
)

_SPEC = ToolSpec(
    name="caura_memory_update",
    title="Update memory",
    description=_DESCRIPTION,
    handler=mcp_server.caura_memory_update,
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
