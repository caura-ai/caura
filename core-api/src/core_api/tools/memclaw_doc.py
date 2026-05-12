"""ToolSpec for memclaw_doc — structured-document CRUD (op-dispatched).

Replaces the 4 prior `memclaw_doc_*` tools. Documents are NOT for
unstructured knowledge — use ``memclaw_write`` for memories.
"""

from core_api import mcp_server

from ._builders import mcp_register
from ._registry import register
from ._types import OpSpec, ToolSpec

_DESCRIPTION = (
    "Structured-document CRUD in named collections. "
    "op: write|read|query|delete|list_collections|search. "
    "write upserts by collection+doc_id — include data['summary'] (1-3 dense "
    "sentences, intent-focused) to make the doc semantically searchable; "
    "omit it to store without indexing. query filters by where dict; "
    "list_collections enumerates every collection this tenant has (with counts); "
    "search runs semantic retrieval over data['summary'] vectors — "
    "pass collection to scope to one (narrow strategy), or omit collection to "
    "span every collection in the tenant (broad strategy). "
    "Use for structured records (customers, config). For memories use memclaw_write."
)

_SPEC = ToolSpec(
    name="memclaw_doc",
    description=_DESCRIPTION,
    handler=mcp_server.memclaw_doc,
    plugin_exposed=True,
    trust_required=0,
    ops=(
        OpSpec(
            name="write",
            description=(
                "Upsert a document by collection+doc_id. Set data['summary'] "
                "to a short (1-3 sentence) intent-focused description to "
                "make the doc semantically searchable — that string (and "
                "only that string) is what gets embedded. The full body "
                "lives wherever the caller puts it (e.g. data['content']) "
                "and is returned in read/search results but is NOT indexed. "
                "Good: 'Postgres tuning runbook: vacuum, autovacuum, work_mem.' "
                "Bad: '<the first 200 chars of the body>' (truncation is not "
                "summarization). "
                "Omit summary to store the doc without indexing."
            ),
            required_params=("collection", "doc_id", "data"),
        ),
        OpSpec(
            name="read",
            description="Fetch a document by collection+doc_id.",
            required_params=("collection", "doc_id"),
        ),
        OpSpec(
            name="query",
            description="Filter documents by field equality.",
            required_params=("collection",),
        ),
        OpSpec(
            name="delete",
            description="Remove a document by collection+doc_id.",
            required_params=("collection", "doc_id"),
        ),
        OpSpec(
            name="list_collections",
            description=(
                "Enumerate every collection this tenant has written to, with "
                "per-collection document counts. No required params; optional "
                "fleet_id scopes counts to one fleet."
            ),
            required_params=(),
        ),
        OpSpec(
            name="search",
            description=(
                "Semantic search. Pass collection to scope to one (narrow); "
                "omit collection to search every collection in the tenant "
                "(broad). Only docs written with a data['summary'] appear "
                "(no summary → no embedding → invisible to search). Returns "
                "up to top_k results ordered by cosine similarity (1.0 = "
                "identical). Each row includes its own collection so the "
                "caller can follow up with op=read."
            ),
            required_params=("query",),
        ),
    ),
    error_codes=("INVALID_ARGUMENTS",),
)
register(_SPEC)
mcp_register(mcp_server.mcp, _SPEC)
