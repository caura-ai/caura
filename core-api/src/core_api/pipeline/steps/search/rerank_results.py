"""RerankResults — optional second-stage reranking of the candidate pool.

Sits between ``ExecuteScoredSearch`` (which produces ``raw_rows``) and
``PostFilterResults`` (which floors + trims). Re-scores the pool with the
configured ranker (``RANK_PROVIDER``) and reorders ``raw_rows`` by the new
scores. This is the ``rerank seam`` from the ranking-component design.

Ships dark: with ``RANK_PROVIDER=noop`` (the default) the ranker returns
first-stage similarity and the reorder is a stable no-op — zero behaviour
change until a deployment/tenant opts in. Any failure (misconfig, timeout,
provider down) returns ``None`` from ``get_ranking`` and this step keeps
the first-stage order. Recall never fails because rerank did.
"""

from __future__ import annotations

from common.ranking import RankCandidate, get_ranking
from common.ranking.constants import RANK_CANDIDATE_LIMIT
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepOutcome, StepResult


class RerankResults:
    @property
    def name(self) -> str:
        return "rerank_results"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        plan = ctx.data.get("retrieval_plan")
        # ENTITY_LOOKUP skipped scored-search and populated filtered_rows
        # directly (no raw_rows / no vector scores) — nothing to rerank.
        if plan and plan.skip_scored_search:
            return StepResult(outcome=StepOutcome.SKIPPED)

        rows = ctx.data.get("raw_rows")
        if not rows:
            return StepResult(outcome=StepOutcome.SKIPPED)

        # Bound cross-encoder compute / remote payload: re-rank the top
        # RANK_CANDIDATE_LIMIT by first-stage order; rows past the cap keep
        # their order, appended after the re-ranked head.
        head = rows[:RANK_CANDIDATE_LIMIT]
        tail = rows[RANK_CANDIDATE_LIMIT:]

        candidates = [
            RankCandidate(
                id=str(r.Memory.id),
                content=(getattr(r.Memory, "content", "") or ""),
                similarity=float(r.similarity) if r.similarity is not None else 0.0,
                features={
                    "vec_sim": getattr(r, "vec_sim", None),
                    "freshness": getattr(r, "freshness", None),
                    "memory_type": getattr(r.Memory, "memory_type", None),
                },
            )
            for r in head
        ]

        scores = await get_ranking(ctx.data["query"], candidates, ctx.data.get("tenant_config"))
        if scores is None:
            # Degraded / noop-with-nothing-to-do: keep first-stage order.
            return None

        order = sorted(range(len(head)), key=lambda i: scores[i], reverse=True)
        ctx.data["raw_rows"] = [head[i] for i in order] + tail
        return None
