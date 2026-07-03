"""GraphReasoningBoost — opt-in agentic reasoning loop over typed graph-query tools (RL-03).

Runs only when ``reasoning_mode`` is set and graph expansion is not disabled.
Additive to ParallelEmbedAndEntityBoost's fixed single-pass boost: unions
memory ids and takes the max() of boost factors, never replaces them — so the
default (reasoning_mode=False) path is byte-for-byte unaffected.
"""

from __future__ import annotations

from uuid import UUID

from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepOutcome, StepResult
from core_api.services.graph_reasoning_loop import run_reasoning_loop


class GraphReasoningBoost:
    @property
    def name(self) -> str:
        return "graph_reasoning_boost"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        data = ctx.data
        if not data.get("reasoning_mode") or not data.get("graph_expand", True):
            return StepResult(outcome=StepOutcome.SKIPPED)

        plan = data.get("retrieval_plan")
        seed_entity_ids = [str(eid) for eid in plan.matched_entity_ids] if plan else []

        boosted_ids, boost_factor, trace = await run_reasoning_loop(
            data["query"],
            data["tenant_id"],
            data.get("tenant_config"),
            seed_entity_ids,
            fleet_ids=data.get("fleet_ids"),
            reference_datetime=data.get("valid_at"),
        )

        if boosted_ids:
            new_ids = {UUID(mid) for mid in boosted_ids}
            existing_ids = data.get("boosted_memory_ids") or set()
            existing_factors = data.get("memory_boost_factor") or {}
            for mid in new_ids:
                if mid not in existing_factors or boost_factor > existing_factors[mid]:
                    existing_factors[mid] = boost_factor
            data["boosted_memory_ids"] = existing_ids | new_ids
            data["memory_boost_factor"] = existing_factors

        data["reasoning_trace"] = trace
        return None
