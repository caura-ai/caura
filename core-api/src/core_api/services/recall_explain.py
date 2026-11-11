"""EC-06 — "why was this memory not found?" for one memory, query and agent.

Runs the real search pipeline (``build_search_explain_pipeline``: the search's
own steps up to the result set, plus ``ExplainMemoryFilters``) and names the
FIRST stage that removed the memory:

    filter stage   storage's row filters, in the order the search applies them,
                   each evaluated by the search's own predicate
    entity lookup  the query matched an entity and the short-circuit returned
                   only that entity's memories, without scoring
    ranking stage  below the similarity floor, excluded as a derived fan-out
                   row, or ranked past ``top_k``

Every judgement reuses the code that made it in the search: the storage
predicates (``_scored_search_row_filters``), the payload builder, the floor
(``relevance_floor_args`` / ``_passes_relevance_filter``) and the derived test
(``_is_derived``). A reimplementation here could disagree with the search and
the explanation would then lie.

No ranking change, no LLM call, and no recall side effect: the explain
composition leaves out recall tracking, recall-event logging and value metering.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from core_api.pipeline.steps.search.post_filter_results import (
    _is_derived,
    _passes_relevance_filter,
    relevance_floor_args,
)
from core_api.pipeline.steps.search.retrieval_types import RetrievalStrategy

# Storage's filter names, in the order the search applies them, with the
# sentence each one means. A filter storage reports that is missing here is
# still reported, under its own name — never dropped.
FILTER_REASONS: dict[str, str] = {
    "tenant_scope": "The memory belongs to a tenant this search does not read.",
    "deleted": "The memory is soft-deleted.",
    "held": "The memory is held (quarantined) for review and is not searchable until released.",
    "embedding_missing": (
        "The memory has no embedding and the query does not keyword-match it; an unembedded "
        "memory is only found by a keyword match."
    ),
    "fleet_scope": "The memory is in a fleet this search is not scoped to.",
    "visibility_scope": "The memory is private (scope_agent) to another agent than the one searching.",
    "agent_filter": "The search only returns memories written by one agent (filter_agent_id), and another agent wrote this one.",
    "memory_type_filter": "The search filters to another memory type.",
    "verified_only": (
        "The search returns only verified memories (verified_only), and this one is unverified or disputed."
    ),
    "baton_id": "The search returns only memories linked to one handoff baton (baton_id), and this one is not.",
    "status_filter": "The search filters to another status.",
    "status_superseded": (
        "The memory is superseded (status outdated or conflicted); search excludes superseded "
        "memories (a conflicted one is kept only on an exact keyword match)."
    ),
    "temporal_valid_at": "The memory becomes valid after the search's valid_at day.",
}

NOT_FOUND = "not_found"
ENTITY_LOOKUP = "entity_lookup_short_circuit"
BELOW_MIN_SIMILARITY = "below_min_similarity"
DERIVED_EXCLUDED = "derived_excluded"
BEYOND_TOP_K = "beyond_top_k"
RETURNED = "returned"
DROPPED_AFTER_RANKING = "dropped_after_ranking"


def classify_explanation(
    data: dict[str, Any],
    memory_id: UUID,
    *,
    reveal_cross_tenant: bool,
) -> dict[str, Any]:
    """Turn the finished explain-pipeline context into the first removal reason.

    ``reveal_cross_tenant``: a memory outside the tenants the search reads is
    reported as ``tenant_scope`` with its fields only to a system admin. Anyone
    else gets ``not_found`` for it, exactly as for an id that does not exist —
    otherwise explain would confirm which ids exist in OTHER tenants.
    """
    target = str(memory_id)
    plan = data.get("retrieval_plan")
    strategy = plan.strategy.value if plan else None
    top_k = data.get("final_top_k") or (data.get("search_params") or {}).get("top_k")
    filters = data.get("explain_filters") or {}
    checks: list[dict[str, Any]] = list(filters.get("checks") or [])
    memory = filters.get("memory")

    out: dict[str, Any] = {
        "memory_id": target,
        "retrieval_strategy": strategy,
        "top_k": top_k,
        "returned": False,
        "stage": None,
        "removed_by": None,
        "reason": None,
        "rank": None,
        "details": {},
        "checks": checks,
        "memory": memory,
    }

    # Returned beats every other answer: the result set is the ground truth.
    results = data.get("results") or []
    for pos, item in enumerate(results, start=1):
        if str(getattr(item, "id", None)) == target:
            out.update(
                returned=True,
                stage="returned",
                removed_by=None,
                reason=f"The memory was returned, at position {pos} of {len(results)}.",
                rank=pos,
            )
            return out

    if not filters.get("exists"):
        out.update(stage="filter", removed_by=NOT_FOUND, reason="No memory with this id exists.", checks=[])
        return out

    failed = next((c for c in checks if not c.get("passed")), None)
    if failed is not None:
        name = str(failed["filter"])
        if name == "tenant_scope" and not reveal_cross_tenant:
            out.update(
                stage="filter",
                removed_by=NOT_FOUND,
                reason="No memory with this id in the tenants this search reads.",
                checks=[],
                memory=None,
            )
            return out
        out.update(
            stage="filter",
            removed_by=name,
            reason=FILTER_REASONS.get(name, f"Removed by the search filter '{name}'."),
            details=_filter_details(name, data, memory or {}),
        )
        return out

    if plan and plan.strategy == RetrievalStrategy.ENTITY_LOOKUP:
        out.update(
            stage="entity_lookup",
            removed_by=ENTITY_LOOKUP,
            reason=(
                "The query matched a known entity, so the search returned only memories linked to "
                "that entity, without scoring; this memory passed every filter but was not among them."
            ),
            details={"matched_entity_ids": [str(e) for e in plan.matched_entity_ids]},
        )
        return out

    raw_rows = data.get("raw_rows") or []
    filtered_ids = {str(r.Memory.id) for r in data.get("filtered_rows") or []}
    min_similarity, _fts_enabled, allow_fts_bypass = relevance_floor_args(data)
    for pos, row in enumerate(raw_rows, start=1):
        if str(row.Memory.id) != target:
            continue
        vec_sim = _round(getattr(row, "vec_sim", None))
        if not _passes_relevance_filter(row, min_similarity, allow_fts_bypass):
            out.update(
                stage="ranking",
                removed_by=BELOW_MIN_SIMILARITY,
                reason=(
                    f"The memory was scored, but its vector similarity {vec_sim} is below the "
                    f"similarity floor {min_similarity}."
                ),
                rank=pos,
                details={"vec_sim": vec_sim, "min_similarity": min_similarity},
            )
        elif not data.get("include_derived", True) and _is_derived(row):
            out.update(
                stage="ranking",
                removed_by=DERIVED_EXCLUDED,
                reason="The memory is an atomic-fact fan-out child, and derived rows are excluded for this search.",
                rank=pos,
            )
        elif target not in filtered_ids:
            out.update(
                stage="ranking",
                removed_by=BEYOND_TOP_K,
                reason=f"The memory passed every filter but ranked {pos}; the search returns the top {top_k}.",
                rank=pos,
                details=_score_details(row),
            )
        else:
            out.update(
                stage="ranking",
                removed_by=DROPPED_AFTER_RANKING,
                reason="The memory survived ranking but was not in the serialized result.",
                rank=pos,
            )
        return out

    rank = data.get("explain_rank")
    window = data.get("explain_rank_window")
    if rank is not None:
        reason = f"The memory passed every filter but ranked {rank}; the search returns the top {top_k}."
    else:
        reason = (
            f"The memory passed every filter but ranked below {window}; the search returns the top {top_k}."
        )
    out.update(
        stage="ranking",
        removed_by=BEYOND_TOP_K,
        reason=reason,
        rank=rank,
        details={"candidate_window": len(raw_rows), "rank_window": window},
    )
    return out


def _filter_details(name: str, data: dict[str, Any], memory: dict[str, Any]) -> dict[str, Any]:
    """The request-side and memory-side values the failed filter compared."""
    if name == "tenant_scope":
        return {"memory_tenant_id": memory.get("tenant_id"), "search_tenant_id": data.get("tenant_id")}
    if name == "fleet_scope":
        return {
            "memory_fleet_id": memory.get("fleet_id"),
            "search_fleet_ids": data.get("fleet_ids"),
            "strict_fleet_scoping": bool(data.get("strict_fleet_scoping")),
        }
    if name == "visibility_scope":
        return {
            "memory_visibility": memory.get("visibility"),
            "memory_agent_id": memory.get("agent_id"),
            "search_agent_id": data.get("caller_agent_id"),
        }
    if name == "agent_filter":
        return {"memory_agent_id": memory.get("agent_id"), "filter_agent_id": data.get("filter_agent_id")}
    if name == "memory_type_filter":
        return {
            "memory_type": memory.get("memory_type"),
            "memory_type_filter": data.get("memory_type_filter"),
        }
    if name == "verified_only":
        return {"verification_status": memory.get("verification_status")}
    if name in ("status_filter", "status_superseded", "held"):
        return {"memory_status": memory.get("status"), "status_filter": data.get("status_filter")}
    if name == "temporal_valid_at":
        return {"memory_valid_from": memory.get("ts_valid_start"), "valid_at": str(data.get("valid_at"))}
    if name == "embedding_missing":
        return {"has_embedding": memory.get("has_embedding")}
    return {}


def _score_details(row: Any) -> dict[str, Any]:
    return {
        "score": _round(getattr(row, "score", None)),
        "vec_sim": _round(getattr(row, "vec_sim", None)),
        "fts_match": bool(getattr(row, "fts_match", False)),
    }


def _round(v: Any) -> float | None:
    return round(float(v), 4) if v is not None else None
