"""PostFilterResults — apply the similarity floor and final result limit."""

from __future__ import annotations

from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepOutcome, StepResult
from core_api.pipeline.steps.search.retrieval_types import RetrievalStrategy
from core_api.search_trim import (
    is_derived_fanout_row,
    passes_relevance_filter,
    trim_reserving_fts_matches,
)


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

        # pm-0918-c-03 — drop atomic-fact fan-out children BEFORE the trim.
        #
        # The placement is the feature. Storage returned
        # ``top_k * SEARCH_OVERFETCH_FACTOR`` candidates and the trim below cuts
        # to ``top_k``, so excluding here consumes overfetch headroom rather than
        # result slots: a caller asking for 50 still gets 50. Dropping the same
        # rows after the trim — or client-side, which is what callers do today —
        # returns 50 minus whatever was dropped and forces the caller to
        # over-fetch and guess. That, not ranking, is the problem this solves.
        #
        # ``include_derived`` is already resolved (request > tenant > global) by
        # ``resolve_include_derived`` in the ctx builder; this step only applies
        # it. Defaults to True via ``.get`` so a context built by an older caller
        # or a test double behaves exactly as it did before this existed.
        #
        # The headroom is not unlimited and is worth stating: at a derived rate
        # above 1 - 1/SEARCH_OVERFETCH_FACTOR (50% at factor 2) the survivors can
        # fall short of top_k and the response is simply shorter. Measured rate
        # on the store that prompted this was ~28%.
        derived_excluded = 0
        if not ctx.data.get("include_derived", True):
            kept = [row for row in filtered if not _is_derived(row)]
            derived_excluded = len(filtered) - len(kept)
            filtered = kept

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
            derived_ids = {id(row) for row in ctx.data["raw_rows"] if _is_derived(row)}
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
                    # Ordered so the derived exclusion is named for what it is.
                    # It runs BEFORE the trim, so a derived row that was cut here
                    # would otherwise be reported as ``trimmed_by_top_k`` — which
                    # is the one reading that sends someone tuning top_k to get
                    # it back, the one thing that cannot work.
                    excluded = "derived_excluded" if id(row) in derived_ids else "trimmed_by_top_k"
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
                        # D12 arm provenance (ann-pool mode): which candidate-pool
                        # arms admitted the row; None off-pool / pre-provenance.
                        "pool_arms": getattr(row, "pool_arms", None),
                        "has_embedding": bool(getattr(row, "has_embedding", True)),
                        "excluded": excluded,
                    }
                )
            ctx.data["diagnostic_results"] = candidates
            ctx.data["diagnostic_counts"] = {
                "candidates_considered": len(ctx.data["raw_rows"]),
                "returned": len(filtered),
                "excluded_below_min_similarity": below_floor,
                "excluded_derived": derived_excluded,
                # ``- derived_excluded`` because those rows never reached the
                # trim. Without it this counter absorbs them and reports a top_k
                # pressure that did not happen, which is the same misreading the
                # per-row ``excluded`` label above avoids.
                "excluded_by_top_k_trim": (
                    len(ctx.data["raw_rows"]) - below_floor - derived_excluded - len(filtered)
                ),
            }
        return None


def _f(v) -> float | None:
    """Round a score factor for the diagnostic trace; None passes through."""
    return round(float(v), 4) if v is not None else None


def _is_derived(row) -> bool:
    """Is this candidate row an atomic-fact fan-out child?

    The shape adapter, and nothing more: the predicate itself lives in
    ``core_api.search_trim`` so the legacy search path applies the identical
    test. Pipeline rows are storage result objects carrying an ORM ``Memory``;
    ``getattr`` twice rather than indexing because a candidate whose metadata
    column is NULL is an ordinary row, not an error.
    """
    return is_derived_fanout_row(getattr(getattr(row, "Memory", None), "metadata_", None))


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
