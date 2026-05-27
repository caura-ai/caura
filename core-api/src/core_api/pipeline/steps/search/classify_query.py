"""ClassifyQuery — classify incoming query into a retrieval strategy.

Examines the query tokens against the entity full-text index.  When entity
matches are found the step short-circuits to an *entity_lookup* strategy
(graph-expanded, scored by hop distance) so downstream embedding and scored
search can be skipped.  Otherwise the query is routed to keyword or semantic
search based on the adaptive FTS weight.
"""

from __future__ import annotations

import asyncio
import logging
import re
import types
from datetime import datetime
from uuid import UUID

from core_api.clients.storage_client import get_storage_client
from core_api.constants import (
    FTS_WEIGHT_BOOSTED,
    GRAPH_HOP_BOOST,
    GRAPH_MAX_BOOSTED_MEMORIES,
    GRAPH_MAX_EXPANDED_ENTITIES,
)
from core_api.middleware.per_tenant_concurrency import per_tenant_storage_slot
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepResult
from core_api.pipeline.steps.search.retrieval_types import (
    RetrievalPlan,
    RetrievalStrategy,
)
from core_api.schemas import EntityLinkOut
from core_api.services.entity_tokens import extract_entity_tokens

_GRAPH_HOP_BOOST_FALLBACK = GRAPH_HOP_BOOST[max(GRAPH_HOP_BOOST)]

_RECENT_CONTEXT_RE = re.compile(
    r"\b(what was i|what did i|my recent|my latest"
    r"|most recent|latest updates?|recent updates?"
    r"|what happened recently|catch me up"
    r"|what have i missed|what did we)\b",
    re.IGNORECASE,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Step
# ---------------------------------------------------------------------------


class ClassifyQuery:
    @property
    def name(self) -> str:
        return "classify_query"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        query: str = ctx.data["query"]
        sp: dict = ctx.data["search_params"]
        tenant_id: str = ctx.data["tenant_id"]
        fleet_ids: list[str] | None = ctx.data.get("fleet_ids")
        fleet_ids = fleet_ids or None  # normalise [] → None for consistent fleet filtering
        caller_agent_id: str | None = ctx.data.get("caller_agent_id")
        filter_agent_id: str | None = ctx.data.get("filter_agent_id")
        memory_type_filter: str | None = ctx.data.get("memory_type_filter")
        status_filter: str | None = ctx.data.get("status_filter")
        valid_at = ctx.data.get("valid_at")
        readable_tenant_ids: list[str] | None = ctx.data.get("readable_tenant_ids")
        graph_max_hops: int = sp["graph_max_hops"]
        top_k: int = sp["top_k"]

        tokens = extract_entity_tokens(query)

        if tokens:
            try:
                sc = get_storage_client()
                matched_ids = await self._entity_fts(sc, tokens, tenant_id, fleet_ids)

                if matched_ids:
                    entity_hops = await self._expand_per_fleet(
                        sc,
                        matched_ids,
                        tenant_id,
                        fleet_ids,
                        graph_max_hops,
                        use_union=True,
                    )

                    filtered_rows = await self._collect_memories(
                        sc,
                        entity_hops,
                        tenant_id,
                        top_k,
                        fleet_ids=fleet_ids,
                        caller_agent_id=caller_agent_id,
                        filter_agent_id=filter_agent_id,
                        memory_type_filter=memory_type_filter,
                        status_filter=status_filter,
                        valid_at=valid_at,
                        readable_tenant_ids=readable_tenant_ids,
                    )

                    if filtered_rows:
                        plan = RetrievalPlan(
                            strategy=RetrievalStrategy.ENTITY_LOOKUP,
                            matched_entity_ids=matched_ids,
                            skip_embedding=True,
                            skip_scored_search=True,
                        )
                        # min_similarity is not applied to entity_lookup results:
                        # these rows are retrieved by graph traversal (hop boost)
                        # rather than vector similarity, so vec_sim is None and the
                        # cosine threshold is not meaningful here.
                        # PostFilterResults will SKIP via its guard.
                        ctx.data["filtered_rows"] = filtered_rows
                        ctx.data["retrieval_plan"] = plan
                        logger.info(
                            "classify_query: entity_lookup (%d entities)",
                            len(matched_ids),
                        )
                        return None
                    # Preserve entity_hops so _entity_boost_pipeline can skip
                    # re-expansion on the keyword/semantic fallthrough path.
                    ctx.data["_classified_entity_hops"] = entity_hops
                    logger.info("classify_query: entity matched but no linked memories, falling through")
            except Exception:
                logger.warning(
                    "classify_query: entity lookup failed, falling back to search",
                    exc_info=True,
                )

        # TEMPORAL: ExtractTemporalHint already set temporal_window upstream.
        temporal_window = ctx.data.get("temporal_window")
        if temporal_window is not None:
            overrides = {
                "freshness_decay_days": max(temporal_window.days, 1),
                "freshness_floor": 0.3,
            }
            plan = RetrievalPlan(
                strategy=RetrievalStrategy.TEMPORAL,
                search_param_overrides=overrides,
            )
            ctx.data["retrieval_plan"] = plan
            logger.info(
                "classify_query: temporal (window=%dd)",
                temporal_window.days,
            )
            return None

        # RECENT_CONTEXT: recency-intent keywords.
        if _RECENT_CONTEXT_RE.search(query):
            overrides = {
                "freshness_decay_days": 7,
                "freshness_floor": 0.2,
                "top_k": min(sp["top_k"], 5),
            }
            plan = RetrievalPlan(
                strategy=RetrievalStrategy.RECENT_CONTEXT,
                search_param_overrides=overrides,
            )
            ctx.data["retrieval_plan"] = plan
            logger.info("classify_query: recent_context")
            return None

        # No entity / temporal / recency match — keyword vs semantic search.
        if sp["fts_weight"] >= FTS_WEIGHT_BOOSTED:
            plan = RetrievalPlan(strategy=RetrievalStrategy.KEYWORD_SEARCH)
            logger.info("classify_query: keyword_search (fts_weight=%.2f)", sp["fts_weight"])
        else:
            plan = RetrievalPlan(strategy=RetrievalStrategy.SEMANTIC_SEARCH)
            logger.info("classify_query: semantic_search (fts_weight=%.2f)", sp["fts_weight"])

        ctx.data["retrieval_plan"] = plan
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _expand_per_fleet(
        sc: object,
        seed_ids: list[UUID],
        tenant_id: str,
        fleet_ids: list[str] | None,
        max_hops: int,
        *,
        use_union: bool = True,
    ) -> dict[UUID, tuple[int, float]]:
        """Call expand_graph per fleet in parallel, merge by keeping lowest hop."""
        ids_to_expand = fleet_ids if fleet_ids else [None]

        results = await asyncio.gather(
            *(
                sc.expand_graph(
                    {
                        "seed_entity_ids": [str(eid) for eid in seed_ids],
                        "tenant_id": tenant_id,
                        "fleet_id": fid,
                        "max_hops": max_hops,
                        "use_union": use_union,
                    }
                )
                for fid in ids_to_expand
            ),
            return_exceptions=True,
        )

        merged: dict[UUID, tuple[int, float]] = {}
        for partial in results:
            if isinstance(partial, BaseException):
                logger.warning("expand_graph failed for a fleet: %s", partial)
                continue
            # Storage returns {entity_id_str: {"hop": int, "weight": float}, ...}
            # (See core-storage-api/.../routers/entities.py expand_graph route.)
            # Positional indexing here used to ``KeyError: 0`` on every call,
            # silently killing the ENTITY_LOOKUP short-circuit. CAURA-684.
            for eid_str, hop_weight in partial.items():
                eid = UUID(eid_str)
                hop, weight = hop_weight["hop"], hop_weight["weight"]
                if (
                    eid not in merged
                    or hop < merged[eid][0]
                    or (hop == merged[eid][0] and weight > merged[eid][1])
                ):
                    merged[eid] = (hop, weight)
        return merged

    @staticmethod
    async def _entity_fts(
        sc: object,
        tokens: list[str],
        tenant_id: str,
        fleet_ids: list[str] | None,
    ) -> list[UUID]:
        """Full-text search against the entity index via storage client."""
        data = {
            "tokens": tokens,
            "tenant_id": tenant_id,
        }
        if fleet_ids:
            data["fleet_ids"] = fleet_ids
        result = await sc.fts_search_entities(data)
        return [UUID(eid) for eid in result]

    @staticmethod
    async def _collect_memories(
        sc: object,
        entity_hops: dict[UUID, tuple[int, float]],
        tenant_id: str,
        top_k: int,
        *,
        fleet_ids: list[str] | None = None,
        caller_agent_id: str | None = None,
        filter_agent_id: str | None = None,
        memory_type_filter: str | None = None,
        status_filter: str | None = None,
        valid_at: datetime | None = None,
        readable_tenant_ids: list[str] | None = None,
    ) -> list[types.SimpleNamespace]:
        """Load memories linked to graph-expanded entities, scored by hop distance."""
        all_entity_ids = list(entity_hops.keys())

        # Cap entity count to bound the query size.
        if len(all_entity_ids) > GRAPH_MAX_EXPANDED_ENTITIES:
            all_entity_ids = sorted(
                all_entity_ids,
                key=lambda eid: (entity_hops[eid][0], -entity_hops[eid][1]),
            )[:GRAPH_MAX_EXPANDED_ENTITIES]

        # Get memory-entity links from storage client.
        # Returns list of {"memory_id", "entity_id", "role"} dicts.
        raw_links = await sc.get_memory_ids_by_entity_ids(
            [str(eid) for eid in all_entity_ids],
        )

        # Sort by hop distance so closest entities are processed first.
        all_links = sorted(
            raw_links,
            key=lambda r: entity_hops.get(UUID(r["entity_id"]), (999, 0.0))[0],
        )

        # Best (lowest hop → highest boost) per memory + collect entity links.
        memory_boost: dict[str, float] = {}
        memory_entity_links: dict[str, list[EntityLinkOut]] = {}
        for link in all_links:
            mem_id, ent_id_str, role = link["memory_id"], link["entity_id"], link.get("role")
            ent_id = UUID(ent_id_str)
            if ent_id not in entity_hops:
                continue
            hop_dist, rel_weight = entity_hops[ent_id]
            boost = GRAPH_HOP_BOOST.get(hop_dist, _GRAPH_HOP_BOOST_FALLBACK) * rel_weight
            if mem_id not in memory_boost or boost > memory_boost[mem_id]:
                memory_boost[mem_id] = boost
            memory_entity_links.setdefault(mem_id, []).append(EntityLinkOut(entity_id=ent_id, role=role))

        if not memory_boost:
            return []

        # Cap to prevent popular-entity fan-out.
        if len(memory_boost) > GRAPH_MAX_BOOSTED_MEMORIES:
            memory_ids_sorted = sorted(memory_boost, key=memory_boost.__getitem__, reverse=True)[
                :GRAPH_MAX_BOOSTED_MEMORIES
            ]
            memory_boost = {mid: memory_boost[mid] for mid in memory_ids_sorted}

        # CAURA-687: load memories by ID via the dedicated short-circuit
        # endpoint. Pre-CAURA-687 this POSTed to /memories/scored-search
        # with a ``memory_ids`` key + ``entity_lookup: True`` flag that
        # route never read; storage hard-indexed body["embedding"], 500'd,
        # and the broad except below swallowed it. The path silently fell
        # through to keyword/semantic on every entity-token query.
        # ``valid_at`` / ``readable_tenant_ids`` are forwarded so this
        # short-circuit's visibility behaviour matches the scored-search
        # fallthrough exactly — drift is a cross-tenant leak risk.
        # top_k is intentionally NOT forwarded: storage returns ALL matching
        # IDs (capped client-side at GRAPH_MAX_BOOSTED_MEMORIES = 50), and
        # the user-facing top_k is applied below AFTER sorting by hop boost.
        # A server-side LIMIT here would discard high-boost rows non-
        # deterministically because the storage query has no ORDER BY.
        search_data: dict = {
            "tenant_id": tenant_id,
            "memory_ids": list(memory_boost.keys()),
            "fleet_ids": fleet_ids,
            "caller_agent_id": caller_agent_id,
            "filter_agent_id": filter_agent_id,
            "memory_type_filter": memory_type_filter,
            "status_filter": status_filter,
        }
        if valid_at is not None:
            search_data["valid_at"] = str(valid_at)
        # Forward readable_tenant_ids whenever the caller's authorised set
        # differs from home-tenant-only. The explicit comparison (rather
        # than `len > 1`) handles the edge case where a single-element
        # list names a tenant other than ``tenant_id``: silently dropping
        # it would degrade to home-tenant reads with no error or log.
        if readable_tenant_ids and readable_tenant_ids != [tenant_id]:
            search_data["readable_tenant_ids"] = readable_tenant_ids
        # Per-tenant storage bulkhead (CAURA-602 follow-up). Same key as
        # the main scored-search step in execute_scored_search.py — a
        # single classify-then-execute pipeline acquires the slot twice
        # (once here, once there) but each acquire/release is bounded
        # to its own storage roundtrip, so there's no deadlock or
        # cumulative latency beyond the time storage actually spends.
        async with per_tenant_storage_slot("storage_search", tenant_id):
            memories = await sc.load_memories_by_ids(search_data)

        # Build result rows with boost scores.
        memories_by_id = {m["id"]: m for m in memories}

        rows = [
            types.SimpleNamespace(
                Memory=types.SimpleNamespace(**memories_by_id[mid]),
                score=boost,
                vec_sim=None,
                entity_links=memory_entity_links.get(mid, []),
            )
            for mid, boost in memory_boost.items()
            if mid in memories_by_id
        ]
        rows.sort(key=lambda r: r.score, reverse=True)
        return rows[:top_k]
