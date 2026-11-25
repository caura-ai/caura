"""ToolSpec for caura_recall — hybrid semantic+keyword search.

Set ``include_brief=true`` to get an LLM-synthesized summary alongside
raw results.
"""

from core_api import mcp_server

from ._builders import mcp_register
from ._registry import register
from ._types import ToolSpec

_DESCRIPTION = (
    "Find memories by meaning (hybrid semantic+keyword). "
    "Set include_brief=true for an LLM summary paragraph. "
    "For non-semantic browse use caura_list; for read-by-id use caura_manage op=read."
)

# The directory surface does not list caura_manage; it reads by id with
# caura_memory_read, so the pointer names that tool there.
#
# Claude clients with many connectors load tools on demand, by searching their
# names and descriptions against what the person asked. "Hybrid semantic +
# keyword" matched nothing a person says, so a question such as "what did we
# decide about pricing?" never found this tool unless the person named Caura.
# This wording says what the tool holds and which questions it answers, in the
# words people use. It describes the tool; it tells the model nothing to do.
_DIRECTORY_DESCRIPTION = (
    "Search the team's shared memory: decisions, facts, outcomes, preferences and "
    "project context that people and agents recorded in earlier sessions. Answers "
    'questions such as "what did we decide about X?", "what do we know about Y?" or '
    '"what happened last time we tried Z?", matching by meaning and by keyword. '
    "Returns the matching memories under items, each with its author (agent_id) and "
    "the author's trust level. Set include_brief=true for a summary paragraph. "
    "For non-semantic browse use caura_list; for read-by-id use caura_memory_read op=read."
)

_SPEC = ToolSpec(
    name="caura_recall",
    title="Recall memories",
    read_only=True,
    description=_DESCRIPTION,
    directory_description=_DIRECTORY_DESCRIPTION,
    handler=mcp_server.caura_recall,
    plugin_exposed=True,
    trust_required=0,
    error_codes=(
        "AGENT_IDENTITY_MISMATCH",
        "AGENT_TRUST_TOO_LOW",
        "BAD_REQUEST",
        "FLEET_SCOPE_FORBIDDEN",
        "FORBIDDEN",
        "INTERNAL_ERROR",
        "INVALID_ARGUMENTS",
        "MISSING_AGENT_ID",
        "RATE_LIMITED",
        "REQUEST_BUDGET_EXCEEDED",
        "TENANT_MISMATCH",
        "UNAUTHORIZED",
        "UNAVAILABLE",
        "UPSTREAM_TIMEOUT",
    ),
)
register(_SPEC)
mcp_register(mcp_server.mcp, _SPEC)
