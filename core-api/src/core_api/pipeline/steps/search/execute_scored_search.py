"""ExecuteScoredSearch — delegate scored search to the storage client.

All scoring expressions, filters, and CTE logic are handled server-side
by core-storage-api.  This step builds the request payload and maps the
response dicts back to SimpleNamespace rows for downstream steps.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from types import SimpleNamespace

from core_api.clients.storage_client import get_storage_client
from core_api.constants import SEARCH_OVERFETCH_FACTOR, SQL_SCORING_PARAM_KEYS
from core_api.middleware.per_tenant_concurrency import per_tenant_storage_slot
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepOutcome, StepResult
from core_api.schemas import EntityLinkOut
from core_api.services.task_tracker import tracked_task
from core_api.tasks import track_task

logger = logging.getLogger(__name__)

_ALLOWED_OVERRIDES = frozenset({"freshness_decay_days", "freshness_floor", "top_k"})


async def _run_ann_pool_shadow(
    sc,
    shadow_search_data: dict,
    primary_snapshot: list[tuple[str, float]],
    *,
    k: int,
    tenant_id: str,
    primary_ms: float,
) -> None:
    """Run the pooled query in the background and log the comparison.

    Compares STORAGE-STAGE rankings at the caller's final_top_k — the raw
    candidate order both paths hand to PostFilterResults — not the
    post-filtered response: the floor and trim apply to both paths alike, and
    comparing before them keeps the metric about what the pool changed. The
    one-line INFO is the rollout gate's per-query evidence; it carries no
    query text (the extract step documents why query text stays out of INFO
    logs) and is grep-shaped: ann_pool_shadow: tenant=... overlap=...

    Runs under the same per-tenant storage bulkhead as a real search — shadow
    load is real load, and exempting it would let shadow-mode tenants park
    unbounded concurrent scans on the reader pool.
    """
    t0 = time.perf_counter()
    async with per_tenant_storage_slot("storage_search", tenant_id):
        shadow_rows = await sc.scored_search(shadow_search_data)
    shadow_ms = (time.perf_counter() - t0) * 1000.0

    primary_ids = [mid for mid, _ in primary_snapshot][:k]
    shadow_ids = [r["id"] for r in shadow_rows][:k]
    p_set, s_set = set(primary_ids), set(shadow_ids)
    overlap = len(p_set & s_set)
    union_n = len(p_set | s_set)
    # Same scope as overlap/jaccard above: ids shared between the two
    # k-truncated windows. Without the [:k] cuts this averaged deltas over
    # the full overfetched candidate sets while the rest of the line spoke
    # about the top-k window — two scopes in one log line.
    primary_scores = dict(primary_snapshot[:k])
    deltas = [
        abs(float(r.get("score") or 0.0) - primary_scores[r["id"]])
        for r in shadow_rows[:k]
        if r["id"] in primary_scores
    ]
    logger.info(
        "ann_pool_shadow: tenant=%s k=%d overlap=%d jaccard=%.3f top1_match=%s "
        "primary_n=%d shadow_n=%d shadow_underfilled=%s "
        "primary_ms=%.0f shadow_ms=%.0f mean_abs_score_delta=%.4f",
        tenant_id,
        k,
        overlap,
        (overlap / union_n) if union_n else 1.0,
        bool(primary_ids and shadow_ids and primary_ids[0] == shadow_ids[0]),
        len(primary_ids),
        len(shadow_ids),
        len(shadow_ids) < min(k, len(primary_ids)),
        primary_ms,
        shadow_ms,
        (sum(deltas) / len(deltas)) if deltas else 0.0,
    )


class ExecuteScoredSearch:
    @property
    def name(self) -> str:
        return "execute_scored_search"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        plan = ctx.data.get("retrieval_plan")
        if plan and plan.skip_scored_search:
            return StepResult(outcome=StepOutcome.SKIPPED)

        data = ctx.data
        sp = data["search_params"]

        # Apply per-strategy overrides (e.g., TEMPORAL tightens freshness_decay_days).
        if plan and plan.search_param_overrides:
            unknown = plan.search_param_overrides.keys() - _ALLOWED_OVERRIDES
            if unknown:
                raise ValueError(f"Unexpected search_param_overrides keys: {unknown}")
            sp = {**sp, **plan.search_param_overrides}

        embedding = data["embedding"]
        temporal_window = data["temporal_window"]
        boosted_memory_ids = data["boosted_memory_ids"]
        memory_boost_factor = data["memory_boost_factor"]
        recall_boost_enabled = data.get("recall_boost_enabled", True)

        # Diagnostic mode: widen the search to capture all candidates.
        diagnostic = data.get("diagnostic", False)
        top_k = sp["top_k"]
        if diagnostic:
            data["diagnostic_original_top_k"] = top_k
            # D12 — diagnostic must not change what the caller gets back:
            # ``final_top_k`` is set so PostFilterResults trims the RESULTS to
            # the requested size exactly as a normal call would, while the
            # widened fetch below feeds the trace with the full candidate set
            # (captured pre-trim in PostFilterResults). Before this, the
            # diagnostic branch skipped ``final_top_k`` — an untrimmed 50-row
            # response whose extra rows would also each get a recall_count
            # bump (TrackRecalls now skips diagnostic calls entirely).
            data["final_top_k"] = top_k
            top_k = max(top_k * SEARCH_OVERFETCH_FACTOR, 50)
        else:
            # Overfetch so PostFilterResults has headroom to drop low-vec_sim rows
            # without starving the final result set. Final trim happens in PostFilterResults.
            data["final_top_k"] = top_k
            top_k = top_k * SEARCH_OVERFETCH_FACTOR

        # ── ANN-pool shadow mode ──
        # ann_pool_shadow=1 alongside ann_pool_size>0: the caller is SERVED
        # the legacy full-scan result while the pooled query runs in the
        # background and the comparison is logged — the rollout gate's
        # evidence on real traffic (the crowding-regime parity analysis in
        # docs/plans/hnsw-two-stage-retrieval.md is why rig numbers alone
        # cannot clear a tenant for cutover). Diagnostic runs are excluded:
        # they widen top_k for the trace and would compare a shape no user is
        # served. Inert while ann_pool_size is 0.
        _ann_pool_size = int(sp.get("ann_pool_size", 0) or 0)
        use_shadow = int(sp.get("ann_pool_shadow", 0) or 0) == 1 and _ann_pool_size > 0 and not diagnostic
        if use_shadow:
            # The primary call crosses the wire with ann_pool_size forced to
            # 0; the shadow payload below restores the configured size. Copy,
            # never mutate — ``sp`` may still be ctx.data["search_params"].
            sp = {**sp, "ann_pool_size": 0}

        # Build the request payload for the storage client.
        #
        # ``search_params`` is PROJECTED through ``SQL_SCORING_PARAM_KEYS`` rather
        # than sent whole: ``ctx.data["search_params"]`` is core-api's working set
        # and a superset of the wire contract, carrying knobs no storage query
        # reads. Projecting at the boundary — rather than removing them at the
        # source — keeps them available to the steps that do read them while
        # guaranteeing they cannot reach the SQL. Why that guarantee is worth
        # having, and what it costs when it is missing: see the tuple's own note.
        #
        # ``if k in sp``: ResolveSearchProfile always writes the full set, but
        # unit-test contexts hand-build a partial one (e.g.
        # tests/test_audit_s6_c1_events.py), so tolerate a subset here and let the
        # storage route's required-key check be the one place that rejects.
        #
        # The diagnostic branch's widening to 50 was defeated by the same
        # shadowing and is restored. D12 wired the mode end-to-end: /search and
        # /recall forward ``SearchRequest.diagnostic``, PostFilterResults writes
        # ``diagnostic_results``/``diagnostic_counts``, the branch above sets
        # ``final_top_k`` so results stay identical, and TrackRecalls skips
        # diagnostic calls.
        search_data: dict = {
            "tenant_id": data["tenant_id"],
            "query": data["query"],
            "embedding": embedding,
            "search_params": {k: sp[k] for k in SQL_SCORING_PARAM_KEYS if k in sp},
            "top_k": top_k,
            "recall_boost_enabled": recall_boost_enabled,
        }

        # Cross-tenant read widening: when the caller's AuthContext is
        # authorised to read beyond its home tenant, pass the full set
        # through to the storage-api so it widens the WHERE predicate.
        # The explicit comparison (rather than ``len > 1``) matches the
        # entity-lookup short-circuit in ``classify_query``: a
        # single-element list naming a tenant other than ``tenant_id``
        # must still be forwarded — ``len > 1`` silently narrowed that
        # caller to home-tenant reads on this path only, so the same
        # request returned different visibility depending on whether the
        # query matched an entity token (audit S6).
        readable = data.get("readable_tenant_ids")
        if readable and readable != [data["tenant_id"]]:
            search_data["readable_tenant_ids"] = readable

        if temporal_window is not None:
            search_data["temporal_window_seconds"] = int(temporal_window.total_seconds())

        if boosted_memory_ids:
            search_data["boosted_memory_ids"] = [str(mid) for mid in boosted_memory_ids]
            search_data["memory_boost_factor"] = {
                str(mid): factor for mid, factor in memory_boost_factor.items()
            }

        fleet_ids = data.get("fleet_ids")
        if fleet_ids:
            search_data["fleet_ids"] = fleet_ids
            # C27 — only meaningful alongside fleet_ids; the storage predicate
            # is built only when a fleet scope was requested.
            if data.get("strict_fleet_scoping"):
                search_data["strict_fleet_scoping"] = True
        if data.get("filter_agent_id"):
            search_data["filter_agent_id"] = data["filter_agent_id"]
        if data.get("caller_agent_id"):
            search_data["caller_agent_id"] = data["caller_agent_id"]
        if data.get("memory_type_filter"):
            search_data["memory_type_filter"] = data["memory_type_filter"]
        if data.get("status_filter"):
            search_data["status_filter"] = data["status_filter"]
        if data.get("valid_at"):
            search_data["valid_at"] = str(data["valid_at"])

        # A63 — a history question must see superseded values: tell the
        # storage side to skip the outdated/conflicted status demotion.
        if data.get("history_hint"):
            search_data["history_query"] = True
            logger.info("execute_scored_search: history query — status demotion lifted")

        date_range = data.get("date_range_filter")
        if date_range:
            search_data["date_range_start"] = date_range["start_date"]
            search_data["date_range_end"] = date_range["end_date"]
            logger.info(
                "execute_scored_search: applying date_range %s → %s",
                date_range["start_date"],
                date_range["end_date"],
            )

        # Per-tenant storage bulkhead (CAURA-602 follow-up). The pipeline
        # search path is the active path (``_USE_PIPELINE_SEARCH=True``);
        # without this slot, the legacy-path bulkhead in
        # ``memory_service._search_memories_legacy`` was applied to dead
        # code only. ``data["tenant_id"]`` is set upstream by
        # ``_search_memories_pipeline`` before this step runs.
        #
        # Unconditional on purpose — this retires C10's
        # ``_storage_slot_acquired`` skip (audit oss-0814-l-06). The slot
        # is an IN-FLIGHT cap held only across the storage roundtrip, not
        # a once-per-request charge: on the entity-lookup fall-through,
        # ``classify_query`` released its slot when its
        # ``load_memories_by_ids`` roundtrip returned, before this step
        # ran, so acquiring here never double-holds — the two roundtrips
        # are sequential and one logical search occupies at most one slot
        # at any instant either way. What the C10 skip actually did was
        # let THIS roundtrip run outside the cap entirely: a tenant whose
        # queries matched entity tokens but fell through (pool loaded,
        # then thinned below top_k by visibility filtering) could park
        # unbounded concurrent scored_search calls on the storage-reader
        # pool — exactly the noisy-neighbor hole the bulkhead exists to
        # close.
        sc = get_storage_client()
        _t0 = time.perf_counter()
        async with per_tenant_storage_slot("storage_search", data["tenant_id"]):
            rows = await sc.scored_search(search_data)
        _primary_ms = (time.perf_counter() - _t0) * 1000.0

        if use_shadow:
            shadow_search_data = dict(search_data)
            shadow_search_data["search_params"] = {
                **search_data["search_params"],
                "ann_pool_size": _ann_pool_size,
            }
            # Snapshot ids+scores now: ``rows`` dicts are mutated into
            # SimpleNamespaces below, and the background task must not hold a
            # reference into pipeline state.
            _primary_snapshot = [(r["id"], float(r.get("score") or 0.0)) for r in rows]
            # Task handle stashed for tests and for /tasks-style draining;
            # tracked_task turns failures into BackgroundTaskLog rows instead
            # of unraised-exception noise.
            data["_ann_shadow_task"] = track_task(
                tracked_task(
                    _run_ann_pool_shadow(
                        sc,
                        shadow_search_data,
                        _primary_snapshot,
                        k=data.get("final_top_k") or sp["top_k"],
                        tenant_id=data["tenant_id"],
                        primary_ms=_primary_ms,
                    ),
                    "ann_pool_shadow",
                    None,
                    data["tenant_id"],
                )
            )

        # Map response dicts to SimpleNamespace rows expected by downstream steps.
        grouped: OrderedDict[str, SimpleNamespace] = OrderedDict()
        for row in rows:
            mid = row["id"]
            if mid not in grouped:
                grouped[mid] = SimpleNamespace(
                    Memory=SimpleNamespace(
                        **{
                            k: v
                            for k, v in row.items()
                            if k
                            not in (
                                "score",
                                "similarity",
                                "vec_sim",
                                "fts_score",
                                "freshness",
                                "entity_boost",
                                "recall_boost",
                                "temporal_boost",
                                "status_penalty",
                                "fts_match",
                                "entity_links",
                                "has_embedding",
                                "pool_arms",
                            )
                        }
                    ),
                    score=row.get("score"),
                    similarity=row.get("similarity"),
                    vec_sim=row.get("vec_sim"),
                    fts_score=row.get("fts_score"),
                    freshness=row.get("freshness"),
                    entity_boost=row.get("entity_boost"),
                    recall_boost=row.get("recall_boost"),
                    temporal_boost=row.get("temporal_boost"),
                    pool_arms=row.get("pool_arms"),
                    status_penalty=row.get("status_penalty"),
                    fts_match=bool(row.get("fts_match", False)),
                    has_embedding=row.get("has_embedding", True),
                    entity_links=[],
                )
            # Entity links may be inline in the row or as a nested list.
            for link in row.get("entity_links", []):
                grouped[mid].entity_links.append(
                    EntityLinkOut(
                        entity_id=link["entity_id"],
                        role=link.get("role"),
                    )
                )

        data["raw_rows"] = list(grouped.values())
        return None
