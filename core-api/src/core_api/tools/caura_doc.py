"""ToolSpec for caura_doc — structured-document CRUD (op-dispatched).

Replaces the 4 prior `caura_doc_*` tools. Documents are NOT for
unstructured knowledge — use ``caura_write`` for memories.

ax-0917-m-15 — that last sentence is advice about which tool to reach for,
not a promise that the two stores stay separate. A write here also mints a
memory carrying the document's data (``services.doc_indexing.resolve_doc_memory``)
and a delete un-mints it, which is why the descriptions below say so: a probe
that read only the line above went looking for its own document and found it
in ``caura_recall`` results instead.
"""

from core_api import mcp_server

from ._builders import mcp_register
from ._registry import register
from ._types import OpSpec, ToolSpec

_DESCRIPTION = (
    "Structured-document CRUD in named collections. "
    "write upserts by collection+doc_id — include data['summary'] (1-3 dense "
    "sentences, intent-focused) to make the doc semantically searchable; "
    "omit it to store without indexing. Every write also mints a memory "
    "carrying the doc's data — so the body is caura_recall-able with or "
    "without a summary — and delete un-mints it; skills and _-prefixed "
    "collections are exempt. query filters by where dict; "
    "list_collections enumerates every collection this tenant has (with counts); "
    "search runs semantic retrieval over data['summary'] vectors — "
    "pass collection to scope to one (narrow strategy), or omit collection to "
    "span every collection in the tenant (broad strategy). "
    "Use for structured records (customers, config). For memories use caura_write."
)

_SPEC = ToolSpec(
    name="caura_doc",
    description=_DESCRIPTION,
    handler=mcp_server.caura_doc,
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
                "Omit summary to store the doc without indexing. "
                "Side effect: the write also mints a memory carrying this "
                "document's data, so the body is reachable by caura_recall — "
                "the doc row embeds only data['summary'], so without it the "
                "body would be findable by meaning nowhere. This does not "
                "depend on summary: a doc with none is invisible to op=search "
                "and still mints. Not minted for collection='skills', "
                "_-prefixed collections, an empty data, or a payload over the "
                "memory size limit."
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
            description="Remove a document by collection+doc_id. Also un-mints its minted memory.",
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
    error_codes=(
        "FORBIDDEN",
        "INTERNAL_ERROR",
        "INVALID_ARGUMENTS",
        "MISSING_AGENT_ID",
        "UNAUTHORIZED",
        "UPSTREAM_ERROR",
    ),
)
register(_SPEC)
mcp_register(mcp_server.mcp, _SPEC)
