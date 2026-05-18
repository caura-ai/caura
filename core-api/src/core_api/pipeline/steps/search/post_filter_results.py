"""PostFilterResults — filter raw rows by min_similarity gate on vec_sim."""

from __future__ import annotations

from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepOutcome, StepResult
from core_api.pipeline.steps.search.retrieval_types import RetrievalStrategy


class PostFilterResults:
    @property
    def name(self) -> str:
        return "post_filter_results"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        plan = ctx.data.get("retrieval_plan")
        if plan and plan.strategy == RetrievalStrategy.ENTITY_LOOKUP:
            return StepResult(outcome=StepOutcome.SKIPPED)

        min_similarity = ctx.data["search_params"]["min_similarity"]
        # NULL-embedding rows reach this point only when storage admitted them
        # via the FTS half of `embedding IS NOT NULL OR search_vector @@ query`
        # (relaxed in CAURA-594 so writes deferred via EMBED_REQUESTED stay
        # searchable during the embed-pending window). Storage coerces their
        # missing cosine to a 0.0 sentinel and emits `has_embedding=False` to
        # disambiguate that from a real orthogonal match — trust that flag
        # here and bypass the vec_sim threshold for FTS-only rows. Without
        # this, the entire FTS-fallback contract is silently broken for any
        # row whose embedding hasn't been PATCHed yet by core-worker.
        filtered = [
            row
            for row in ctx.data["raw_rows"]
            if (not getattr(row, "has_embedding", True))
            or row.vec_sim is None
            or float(row.vec_sim) >= min_similarity
        ]
        # Trim to the user-requested top_k (storage returned top_k * overfetch_factor)
        final_top_k = ctx.data.get("final_top_k")
        if final_top_k is not None:
            filtered = filtered[:final_top_k]
        ctx.data["filtered_rows"] = filtered
        return None
