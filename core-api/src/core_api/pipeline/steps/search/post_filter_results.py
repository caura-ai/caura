"""PostFilterResults — apply the similarity floor and final result limit."""

from __future__ import annotations

from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepOutcome, StepResult
from core_api.pipeline.steps.search.retrieval_types import RetrievalStrategy
from core_api.search_trim import passes_relevance_filter, trim_reserving_fts_matches


class PostFilterResults:
    @property
    def name(self) -> str:
        return "post_filter_results"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        plan = ctx.data.get("retrieval_plan")
        if plan and plan.strategy == RetrievalStrategy.ENTITY_LOOKUP:
            return StepResult(outcome=StepOutcome.SKIPPED)

        min_similarity = ctx.data["search_params"]["min_similarity"]
        fts_enabled = float(ctx.data["search_params"].get("fts_weight", 0.0)) > 0.0
        allow_fts_bypass = fts_enabled and bool(ctx.data.get("allow_fts_global_floor_bypass", False))
        filtered = [
            row
            for row in ctx.data["raw_rows"]
            if _passes_relevance_filter(row, min_similarity, allow_fts_bypass)
        ]
        below_floor = len(ctx.data["raw_rows"]) - len(filtered)
        # Trim to the user-requested top_k (storage returned top_k * overfetch_factor)
        final_top_k = ctx.data.get("final_top_k")
        if final_top_k is not None:
            filtered = trim_reserving_fts_matches(
                filtered,
                final_top_k,
                lambda row: _is_reservable_fts_match(row, fts_enabled),
            )
        ctx.data["filtered_rows"] = filtered

        # D12 — diagnostic trace: capture the FULL widened candidate set with
        # per-row score factors and the reason each cut row was cut, before the
        # trimmed rows are forgotten. Written here (not in a separate step)
        # because this is the one place that knows both the floor and the trim.
        if ctx.data.get("diagnostic"):
            kept_ids = {id(row) for row in filtered}
            passed_floor_ids = set()
            for row in ctx.data["raw_rows"]:
                if _passes_relevance_filter(row, min_similarity, allow_fts_bypass):
                    passed_floor_ids.add(id(row))
            candidates = []
            for row in ctx.data["raw_rows"]:
                m = row.Memory
                excluded = None
                if id(row) not in passed_floor_ids:
                    excluded = "below_min_similarity"
                elif id(row) not in kept_ids:
                    excluded = "trimmed_by_top_k"
                candidates.append(
                    {
                        "id": str(getattr(m, "id", None)),
                        "title": getattr(m, "title", None),
                        "memory_type": getattr(m, "memory_type", None),
                        "status": getattr(m, "status", None),
                        "score": _f(getattr(row, "score", None)),
                        "vec_sim": _f(getattr(row, "vec_sim", None)),
                        "fts_score": _f(getattr(row, "fts_score", None)),
                        "fts_match": bool(getattr(row, "fts_match", False)),
                        "fts_global_floor_bypass": _used_fts_global_floor_bypass(
                            row,
                            min_similarity,
                            allow_fts_bypass,
                        ),
                        "freshness": _f(getattr(row, "freshness", None)),
                        "entity_boost": _f(getattr(row, "entity_boost", None)),
                        "recall_boost": _f(getattr(row, "recall_boost", None)),
                        "temporal_boost": _f(getattr(row, "temporal_boost", None)),
                        "status_penalty": _f(getattr(row, "status_penalty", None)),
                        "has_embedding": bool(getattr(row, "has_embedding", True)),
                        "excluded": excluded,
                    }
                )
            ctx.data["diagnostic_results"] = candidates
            ctx.data["diagnostic_counts"] = {
                "candidates_considered": len(ctx.data["raw_rows"]),
                "returned": len(filtered),
                "excluded_below_min_similarity": below_floor,
                "excluded_by_top_k_trim": len(ctx.data["raw_rows"]) - below_floor - len(filtered),
            }
        return None


def _f(v) -> float | None:
    """Round a score factor for the diagnostic trace; None passes through."""
    return round(float(v), 4) if v is not None else None


def _passes_relevance_filter(
    row,
    min_similarity: float,
    allow_fts_global_floor_bypass: bool,
) -> bool:
    return passes_relevance_filter(
        has_embedding=getattr(row, "has_embedding", True),
        vec_sim=row.vec_sim,
        min_similarity=min_similarity,
        fts_match=bool(getattr(row, "fts_match", False)),
        allow_fts_global_floor_bypass=allow_fts_global_floor_bypass,
    )


def _used_fts_global_floor_bypass(
    row,
    min_similarity: float,
    allow_fts_global_floor_bypass: bool,
) -> bool:
    vec_sim = getattr(row, "vec_sim", None)
    return bool(
        allow_fts_global_floor_bypass
        and getattr(row, "has_embedding", True)
        and getattr(row, "fts_match", False)
        and vec_sim is not None
        and float(vec_sim) < min_similarity
    )


def _is_reservable_fts_match(row, fts_enabled: bool) -> bool:
    """Include embedded matches only while keyword scoring is enabled."""
    return (not getattr(row, "has_embedding", True)) or (
        fts_enabled and bool(getattr(row, "fts_match", False))
    )
