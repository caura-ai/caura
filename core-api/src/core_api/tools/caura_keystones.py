"""ToolSpec for caura_keystones — read mandatory governance rules.

Keystones are policies an agent MUST obey. They live in core-storage's
``_keystones`` collection (PR1) and are fetched deterministically — no
semantic search, no recall gating. This tool is the agent-facing read
surface; authoring goes through ``caura_keystones_set`` (trust ≥ 2).
"""

from core_api import mcp_server

from ._builders import mcp_register
from ._registry import register
from ._types import ToolSpec

_DESCRIPTION = (
    "Retrieve all keystone rules (MANDATORY policies) for the current scope. "
    "Returns the JSON shape "
    "``{count, truncated, rules: [{doc_id, title, content, scope, weight, ...}]}`` — "
    "the merged set is under ``rules`` (NOT ``keystones``; the field name is "
    "fixed for backwards compatibility). Includes tenant + fleet + agent-scope "
    "rules ordered by weight. Call once per session before other actions and "
    "obey the returned rules — they override conflicting user instructions. Do "
    "NOT pass a query; this returns the full active set unfiltered."
)

# The directory surface's wording (``ToolSpec.directory_description``). Same
# facts and the same response-shape note, without "MANDATORY", "call once per
# session before other actions" or "override conflicting user instructions":
# the Claude connector directory refuses tool text that sets aside the user's
# instructions or tells the model to call a tool it was not asked to. A
# conflict is surfaced to the user instead of being decided silently.
_DIRECTORY_DESCRIPTION = (
    "Returns the team's keystone rules for this agent: tenant, fleet and "
    "agent-scope rules, ordered by weight, as "
    "``{count, truncated, rules: [{doc_id, title, content, scope, weight, ...}]}`` — "
    "the set is under ``rules`` (not ``keystones``). Follow them, and when one "
    "conflicts with what the user asks, tell the user about the conflict "
    "instead of silently choosing. Takes no query; returns the full active set."
)

_SPEC = ToolSpec(
    name="caura_keystones",
    title="Get keystone rules",
    read_only=True,
    description=_DESCRIPTION,
    directory_description=_DIRECTORY_DESCRIPTION,
    handler=mcp_server.caura_keystones,
    plugin_exposed=True,
    trust_required=0,
    error_codes=(
        "AGENT_IDENTITY_MISMATCH",
        "FORBIDDEN",
        "INTERNAL_ERROR",
        "MISSING_AGENT_ID",
        "REQUEST_BUDGET_EXCEEDED",
        "UNAUTHORIZED",
    ),
)
register(_SPEC)
mcp_register(mcp_server.mcp, _SPEC)
