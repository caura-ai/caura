"""ExplainMemoryFilters — EC-06: why did ONE memory not come back?

Runs only in the explain pipeline (``build_search_explain_pipeline``), right
after ``ExecuteScoredSearch``, and only when ``ctx.data["explain_memory_id"]``
is set. It asks storage to evaluate the scored search's own row filters
against that memory — the payload is built by the same
``build_scored_search_payload`` the search used, so the filters cannot drift
from the ones that ran.

When every filter passes but the memory is outside the candidate window the
search fetched, it runs one widened scoring pass (the search's own payload,
larger ``top_k``) to find the memory's rank. That is a second storage read of the
same statement, not a different ranking: no LLM, no rerank, no side effect.

Writes ``ctx.data["explain_filters"]`` (storage's per-filter answer) and, when
measured, ``ctx.data["explain_rank"]`` (1-based rank in the widened pass, or
None when the memory is not in it).
"""

from __future__ import annotations

from core_api.clients.storage_client import get_storage_client
from core_api.middleware.per_tenant_concurrency import per_tenant_storage_slot
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepOutcome, StepResult
from core_api.pipeline.steps.search.execute_scored_search import build_scored_search_payload

# How deep the rank probe looks when the memory passed every filter but was not
# in the search's own candidate window. Deep enough to tell "rank 60" from
# "nowhere near", bounded so one explain cannot become a full-tenant scan.
EXPLAIN_RANK_WINDOW = 500


class ExplainMemoryFilters:
    @property
    def name(self) -> str:
        return "explain_memory_filters"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        memory_id = ctx.data.get("explain_memory_id")
        if not memory_id:
            return StepResult(outcome=StepOutcome.SKIPPED)

        data = ctx.data
        # The exact body the search sent, stashed by ExecuteScoredSearch. The
        # ENTITY_LOOKUP short-circuit skips that step, so build it the same way
        # here; the embed/boost keys it reads can then be absent, and they
        # shape scoring only, never a filter.
        payload = data.get("scored_search_payload")
        if payload is None:
            sp = data["search_params"]
            payload_ctx = {
                "embedding": None,
                "temporal_window": None,
                "boosted_memory_ids": set(),
                "memory_boost_factor": {},
                **data,
            }
            payload = build_scored_search_payload(payload_ctx, sp, sp["top_k"])

        sc = get_storage_client()
        async with per_tenant_storage_slot("storage_search", data["tenant_id"]):
            filters = await sc.explain_scored_search_filters({**payload, "memory_id": str(memory_id)})
        data["explain_filters"] = filters

        plan = data.get("retrieval_plan")
        if plan and plan.skip_scored_search:
            return None
        if not filters.get("exists") or not all(c["passed"] for c in filters.get("checks", [])):
            return None
        target = str(memory_id)
        if any(str(row.Memory.id) == target for row in data.get("raw_rows") or []):
            return None

        async with per_tenant_storage_slot("storage_search", data["tenant_id"]):
            rows = await sc.scored_search({**payload, "top_k": EXPLAIN_RANK_WINDOW})
        data["explain_rank"] = next((i + 1 for i, r in enumerate(rows) if str(r["id"]) == target), None)
        data["explain_rank_window"] = EXPLAIN_RANK_WINDOW
        return None
