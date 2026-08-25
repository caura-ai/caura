"""PostFilterResults — filter raw rows by min_similarity gate on vec_sim."""

from __future__ import annotations

from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepOutcome, StepResult
from core_api.pipeline.steps.search.retrieval_types import RetrievalStrategy
from core_api.search_trim import trim_reserving_fts_only


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
        below_floor = len(ctx.data["raw_rows"]) - len(filtered)
        # Trim to the user-requested top_k (storage returned top_k * overfetch_factor)
        final_top_k = ctx.data.get("final_top_k")
        if final_top_k is not None:
            filtered = trim_reserving_fts_only(filtered, final_top_k, _is_fts_only)
        ctx.data["filtered_rows"] = filtered

        # D12 — diagnostic trace: capture the FULL widened candidate set with
        # per-row score factors and the reason each cut row was cut, before the
        # trimmed rows are forgotten. Written here (not in a separate step)
        # because this is the one place that knows both the floor and the trim.
        if ctx.data.get("diagnostic"):
            kept_ids = {id(row) for row in filtered}
            passed_floor_ids = set()
            for row in ctx.data["raw_rows"]:
                if (
                    (not getattr(row, "has_embedding", True))
                    or row.vec_sim is None
                    or float(row.vec_sim) >= min_similarity
                ):
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


def _is_fts_only(row) -> bool:
    """True for a row storage admitted on FTS alone, with no embedding yet.

    Same test as the cosine-gate exemption above, which has used this form since
    CAURA-679 — kept identical so the gate and the reservation cannot disagree
    about what "FTS-only" means.

    ``row`` is a ``SimpleNamespace``, hence ``getattr``; the legacy path in
    ``memory_service`` holds dicts and reads the same field with ``.get``. The
    value is always a real ``bool``, never ``None``: storage computes it as
    ``Memory.embedding.is_not(None)`` — a non-nullable SQL boolean — and
    ``execute_scored_search`` carries it onto the namespace with a ``True``
    default. So truthiness and ``is False`` cannot diverge on any reachable
    input here.
    """
    return not getattr(row, "has_embedding", True)
