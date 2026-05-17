"""ExecuteScoredSearch — delegate scored search to the storage client.

All scoring expressions, filters, and CTE logic are handled server-side
by core-storage-api.  This step builds the request payload and maps the
response dicts back to SimpleNamespace rows for downstream steps.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from types import SimpleNamespace

from core_api.clients.storage_client import get_storage_client
from core_api.constants import SEARCH_OVERFETCH_FACTOR
from core_api.middleware.per_tenant_concurrency import per_tenant_storage_slot
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepOutcome, StepResult
from core_api.schemas import EntityLinkOut

logger = logging.getLogger(__name__)

_ALLOWED_OVERRIDES = frozenset({"freshness_decay_days", "freshness_floor", "top_k"})


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
            top_k = max(top_k, 50)
        else:
            # Overfetch so PostFilterResults has headroom to drop low-vec_sim rows
            # without starving the final result set. Final trim happens in PostFilterResults.
            data["final_top_k"] = top_k
            top_k = top_k * SEARCH_OVERFETCH_FACTOR

        # Build the request payload for the storage client.
        search_data: dict = {
            "tenant_id": data["tenant_id"],
            "query": data["query"],
            "embedding": embedding,
            "search_params": sp,
            "top_k": top_k,
            "recall_boost_enabled": recall_boost_enabled,
        }

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
        sc = get_storage_client()
        async with per_tenant_storage_slot("storage_search", data["tenant_id"]):
            rows = await sc.scored_search(search_data)

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
                                "entity_links",
                                "has_embedding",
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
                    status_penalty=row.get("status_penalty"),
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
