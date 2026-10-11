"""RecordRecallValue — the recall savings Caura can stand behind (usage metering).

For each search that returned memories, records (``common.usage.record_value``):

* ``recall.count`` / ``recall.results`` — how many recalls, and results.
* ``recall.returned_tokens`` — the tokens Caura handed back, counted exactly.
* ``recall.derived_returned_tokens`` / ``recall.derived_source_tokens`` — for
  results that are a piece of a larger memory (an auto-chunk or an atomic
  fact, both naming their parent through the reserved ``parent_memory_id``),
  the tokens returned and the FULL size of the distinct parents they came
  from. The difference is the context saved: without the store, the agent
  would have had to send the whole parent. A whole memory returned as itself
  saves nothing and adds nothing here.
* ``recall.cross_agent_results`` — results another agent wrote: shared memory
  in use.

Not counted, deliberately: "tokens saved versus sending every memory" (nobody
would) and anything we cannot observe. Diagnostic searches (Prism's map) are
not recalls and record nothing.

Runs after the response, fire-and-forget like ``LogRecallEvent``: the parent
sizes cost one storage read, and none of this may slow or break a recall.
Tokens are tiktoken ``cl100k_base``, the tokenizer core-api already ships.
"""

from __future__ import annotations

import logging
from typing import Any

from common.usage import record_value, usage_scope
from core_api.clients.storage_client import get_storage_client
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepResult
from core_api.services.ingest_chunking import doc_token_count
from core_api.tasks import track_task

logger = logging.getLogger(__name__)

#: The two writers that put ``parent_memory_id`` on a child (search_trim.py
#: explains why both halves are checked: the key is reserved, ``source`` is not).
_DERIVED_SOURCES = frozenset({"atomic_fact_fanout", "auto_chunk"})


def _field(row: Any, name: str) -> Any:
    return row.get(name) if isinstance(row, dict) else getattr(row, name, None)


def parent_of(metadata: Any) -> str | None:
    """The parent memory a derived result came from, else None."""
    if not isinstance(metadata, dict):
        return None
    raw = metadata.get("_system")
    nested: dict = raw if isinstance(raw, dict) else {}
    parent = metadata.get("parent_memory_id") or nested.get("parent_memory_id")
    source = nested.get("source", metadata.get("source"))
    return str(parent) if parent and source in _DERIVED_SOURCES else None


def summarize(results: list[Any], caller_agent_id: str | None) -> tuple[dict[str, int], set[str]]:
    """The per-recall measures that need no storage read, and the parents to size."""
    measures = {
        "recall.count": 1,
        "recall.results": len(results),
        "recall.returned_tokens": 0,
        "recall.derived_returned_tokens": 0,
        "recall.cross_agent_results": 0,
    }
    parents: set[str] = set()
    for row in results:
        tokens = doc_token_count(_field(row, "content") or "")
        measures["recall.returned_tokens"] += tokens
        parent = parent_of(_field(row, "metadata"))
        if parent:
            parents.add(parent)
            measures["recall.derived_returned_tokens"] += tokens
        author = _field(row, "agent_id")
        if caller_agent_id and author and author != caller_agent_id:
            measures["recall.cross_agent_results"] += 1
    return measures, parents


async def _record(tenant_id: str, operation: str, measures: dict[str, int], parents: set[str]) -> None:
    try:
        with usage_scope(operation=operation):
            for metric, quantity in measures.items():
                record_value(metric, quantity)
            if not parents:
                return
            rows = await get_storage_client().bulk_get_memories(sorted(parents), tenant_id)
            # A parent deleted since counts as nothing: no size, no claim.
            source = sum(doc_token_count(r.get("content") or "") for r in rows if r)
            if source:
                record_value("recall.derived_source_tokens", source)
    except Exception:
        logger.warning("recall savings not recorded", exc_info=True)


def schedule_recall_value(
    results: list[Any], *, tenant_id: str | None, caller_agent_id: str | None, source: str | None
) -> None:
    """Record one recall's savings after the response. Shared by both search paths."""
    try:
        if not results or not tenant_id:
            return
        measures, parents = summarize(results, caller_agent_id)
        track_task(_record(tenant_id, source or "search", measures, parents))
    except Exception:
        logger.warning("recall savings skipped due to error", exc_info=True)


class RecordRecallValue:
    @property
    def name(self) -> str:
        return "record_recall_value"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        if not ctx.data.get("diagnostic"):
            schedule_recall_value(
                ctx.data.get("results") or [],
                tenant_id=ctx.data.get("tenant_id"),
                caller_agent_id=ctx.data.get("caller_agent_id"),
                source=ctx.data.get("source"),
            )
        return None
