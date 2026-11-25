"""ToolSpec for caura_write — single OR batch.

Provide exactly one of {content, items}. The system auto-classifies type,
weight, title, summary, and temporal dates.
"""

from core_api import mcp_server

from ._builders import mcp_register
from ._registry import register
from ._types import ToolSpec

_DESCRIPTION = (
    "Store NEW memories. Provide exactly one of {content, items} (batch ≤100). "
    "System auto-classifies type, importance, title, dates. "
    "visibility: scope_team (default) / scope_agent / scope_org (trust ≥ 3). Prefer team for sharing. "
    "If the response has metadata.embedding_pending=true the memory is saved but not yet "
    "semantically searchable (~15-20s); it is already findable by keyword and in caura_list, "
    "so do not re-write it. Pass write_mode='strong' to embed inline when you must recall it now. "
    "Unknown keys are rejected, not ignored: put your own under metadata."
)

# The directory surface's wording (``ToolSpec.directory_description``): what the
# tool keeps, in the words people use, so a client that finds tools by search
# matches "remember that..." or "save this decision". Then the default text.
_DIRECTORY_DESCRIPTION = (
    "Save to the team's shared memory: decisions, facts, outcomes, preferences and "
    "project context worth keeping for later sessions and for teammates, such as "
    '"remember that we chose X" or "note that Y did not work". '
) + _DESCRIPTION

_SPEC = ToolSpec(
    name="caura_write",
    title="Write memories",
    description=_DESCRIPTION,
    directory_description=_DIRECTORY_DESCRIPTION,
    handler=mcp_server.caura_write,
    plugin_exposed=True,
    trust_required=0,
    error_codes=(
        "AGENT_NOT_APPROVED",
        "BAD_REQUEST",
        "BATCH_TOO_LARGE",
        "CONFLICT",
        "DUPLICATE_MEMORY",
        "FLEET_SCOPE_FORBIDDEN",
        "FORBIDDEN",
        "INTERNAL_ERROR",
        "INVALID_ARGUMENTS",
        "INVALID_BATCH_ITEM",
        "MISSING_AGENT_ID",
        "PLAN_LIMIT_READ_ONLY",
        "RATE_LIMITED",
        "REQUEST_BUDGET_EXCEEDED",
        "UNAUTHORIZED",
        "UNAVAILABLE",
        "UPSTREAM_TIMEOUT",
    ),
)
register(_SPEC)
mcp_register(mcp_server.mcp, _SPEC)
