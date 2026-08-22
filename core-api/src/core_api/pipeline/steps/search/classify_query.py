"""ClassifyQuery — classify incoming query into a retrieval strategy.

Examines the query tokens against the entity full-text index.  When entity
matches are found the step MAY short-circuit to an *entity_lookup* strategy
(graph-expanded, scored by hop distance) so downstream embedding and scored
search can be skipped — but only when the linked-memory pool can fill the
caller's ``top_k``.  That route replaces scoring rather than re-ranking it, so
an under-filled pool falls through instead and the entity hits are applied as a
hop boost over real scores (H-03).  Otherwise the query is routed to keyword or
semantic search based on the adaptive FTS weight.
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
    ENTITY_LOOKUP_MAX_MATCHES,
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
        search_params: dict = ctx.data["search_params"]
        tenant_id: str = ctx.data["tenant_id"]
        fleet_ids: list[str] | None = ctx.data.get("fleet_ids")
        fleet_ids = fleet_ids or None  # normalise [] → None for consistent fleet filtering
        caller_agent_id: str | None = ctx.data.get("caller_agent_id")
        filter_agent_id: str | None = ctx.data.get("filter_agent_id")
        memory_type_filter: str | None = ctx.data.get("memory_type_filter")
        status_filter: str | None = ctx.data.get("status_filter")
        valid_at = ctx.data.get("valid_at")
        readable_tenant_ids: list[str] | None = ctx.data.get("readable_tenant_ids")
        graph_max_hops: int = search_params["graph_max_hops"]
        top_k: int = search_params["top_k"]

        # ``search.entity_retrieval`` org setting (default True — absent key on
        # any internal caller means "enabled", so behaviour is unchanged). When
        # off, skip the entity block entirely: no entity FTS, no graph
        # expansion, and no ENTITY_LOOKUP short-circuit, so the query falls
        # through to the temporal / recent-context / keyword / semantic cascade
        # below. ``ParallelEmbedAndEntityBoost`` reads the same flag and skips
        # hop-boosting, making this the single switch for query-time entity and
        # graph retrieval. Deliberately independent of ``graph_expand``
        # (``search.graph_retrieval``), which only bounds expansion depth once
        # an entity has already matched.
        entity_retrieval: bool = ctx.data.get("entity_retrieval", True)

        tokens = extract_entity_tokens(query) if entity_retrieval else []

        if not entity_retrieval:
            logger.info("classify_query: entity retrieval disabled by org setting (tenant=%s)", tenant_id)

        if tokens:
            try:
                sc = get_storage_client()
                matched_ids = await self._entity_fts(sc, tokens, tenant_id, fleet_ids)

                # CAURA-698: over-broad match → not a "name a specific entity"
                # query. The precision argument for entity_lookup breaks down
                # at high match counts: graph expansion + memory linking
                # against a dense entity index return broadly-related-but-
                # low-relevance results, and the rest of the pipeline (vector
                # scoring, FTS rank, freshness) is skipped under the short-
                # circuit so the noise can't be re-filtered. Bail to the
                # keyword/semantic cascade instead.
                if matched_ids and len(matched_ids) > ENTITY_LOOKUP_MAX_MATCHES:
                    logger.info(
                        "classify_query: entity_lookup short-circuit declined "
                        "(%d matches > threshold %d), falling through",
                        len(matched_ids),
                        ENTITY_LOOKUP_MAX_MATCHES,
                    )
                    # The same over-broad match must not be re-derived by
                    # ParallelEmbedAndEntityBoost and used for hop-boosting:
                    # with N >> GRAPH_MAX_BOOSTED_MEMORIES sibling entities all
                    # at hop 0, the boost degenerates into an arbitrary-50
                    # lottery that buries rows pure scoring ranks first
                    # (S1 @K=10000: 11/25 vs rank-1 on unboosted score).
                    ctx.data["entity_match_declined"] = True
                    matched_ids = []

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
                        query=query,
                        fleet_ids=fleet_ids,
                        caller_agent_id=caller_agent_id,
                        filter_agent_id=filter_agent_id,
                        memory_type_filter=memory_type_filter,
                        status_filter=status_filter,
                        valid_at=valid_at,
                        readable_tenant_ids=readable_tenant_ids,
                        slot_acquired_marker=ctx.data,
                    )

                    # H-03: the short-circuit REPLACES scoring rather than
                    # re-ranking it, so it is only defensible while the entity
                    # pool can answer the request on its own. Below top_k it made
                    # the result strictly worse than never matching an entity:
                    # memories outside the link set were unreachable at ANY top_k,
                    # and the pool is itself cut to GRAPH_MAX_BOOSTED_MEMORIES
                    # before content loads, so the relevant memory could be
                    # dropped before top_k was even applied.
                    #
                    # ``filtered_rows`` is already ``rows[:top_k]``, so this can
                    # only ever be equality — the real gate is the pre-load pool
                    # check in _collect_memories, and this re-checks it because
                    # visibility filtering there can still return short. Keep the
                    # two in step if _collect_memories ever gains an overfetch
                    # bound, or this silently becomes always-true.
                    #
                    # The truthiness term is not redundant: it keeps a zero-row
                    # pool from short-circuiting were top_k ever 0.
                    if filtered_rows and len(filtered_rows) >= top_k:
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
                            "classify_query: entity_lookup (%d entities, pool filled top_k=%d)",
                            len(matched_ids),
                            top_k,
                        )
                        return None
                    # Distinct wording from the over-broad decline logged above,
                    # deliberately: the two have opposite downstream effects (that
                    # one sets entity_match_declined to SUPPRESS hop-boosting, this
                    # one must not), so ops has to be able to grep them apart. Same
                    # reasoning as parallel_embed_entity_boost's distinct reasons.
                    # Four reachable declines, four messages — ordered so each is
                    # reached only in its own state. An empty result is ambiguous
                    # on its own (never loaded vs loaded and wholly filtered), so
                    # _collect_memories reports both the pool size and whether it
                    # got as far as the load.
                    pool_size = ctx.data.pop("_entity_pool_size", None)
                    pool_loaded = ctx.data.pop("_entity_pool_loaded", False)
                    if filtered_rows:
                        # Pool looked adequate before the load and came back short
                        # anyway — visibility filtering (caller_agent_id / status /
                        # valid_at) dropped rows. This is the case the post-load
                        # re-check exists for, so it must not be reported as the
                        # empty one.
                        logger.info(
                            "classify_query: entity_lookup declined as under-filled after "
                            "visibility filtering (%d rows < top_k=%d), falling through",
                            len(filtered_rows),
                            top_k,
                        )
                    elif pool_loaded:
                        # Links existed and the load ran, but visibility filtering
                        # (caller_agent_id / status / valid_at) dropped every row.
                        # A permission or lifecycle cause, not a graph one.
                        logger.info(
                            "classify_query: entity_lookup declined — loaded a pool of %d "
                            "but visibility filtering dropped every row, falling through",
                            pool_size or 0,
                        )
                    elif pool_size:
                        logger.info(
                            "classify_query: entity_lookup declined as under-filled "
                            "(pool %d < top_k=%d), falling through",
                            pool_size,
                            top_k,
                        )
                    else:
                        logger.info("classify_query: entity matched but no linked memories, falling through")
                    # Preserve entity_hops so _entity_boost_pipeline can skip
                    # re-expansion on the keyword/semantic fallthrough path.
                    ctx.data["_classified_entity_hops"] = entity_hops
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
                "top_k": min(search_params["top_k"], 5),
            }
            plan = RetrievalPlan(
                strategy=RetrievalStrategy.RECENT_CONTEXT,
                search_param_overrides=overrides,
            )
            ctx.data["retrieval_plan"] = plan
            logger.info("classify_query: recent_context")
            return None

        # No entity / temporal / recency match — keyword vs semantic search.
        if search_params["fts_weight"] >= FTS_WEIGHT_BOOSTED:
            plan = RetrievalPlan(strategy=RetrievalStrategy.KEYWORD_SEARCH)
            logger.info("classify_query: keyword_search (fts_weight=%.2f)", search_params["fts_weight"])
        else:
            plan = RetrievalPlan(strategy=RetrievalStrategy.SEMANTIC_SEARCH)
            logger.info("classify_query: semantic_search (fts_weight=%.2f)", search_params["fts_weight"])

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
        query: str = "",
        fleet_ids: list[str] | None = None,
        caller_agent_id: str | None = None,
        filter_agent_id: str | None = None,
        memory_type_filter: str | None = None,
        status_filter: str | None = None,
        valid_at: datetime | None = None,
        readable_tenant_ids: list[str] | None = None,
        slot_acquired_marker: dict | None = None,
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
        # ``memory_match_count`` tracks how many DIRECTLY-MATCHED (hop-0) query
        # entities each memory links to — the pre-load relevance signal that
        # survives the fan-out cap below.
        memory_boost: dict[str, float] = {}
        memory_match_count: dict[str, int] = {}
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
            if hop_dist == 0:
                memory_match_count[mem_id] = memory_match_count.get(mem_id, 0) + 1
            memory_entity_links.setdefault(mem_id, []).append(EntityLinkOut(entity_id=ent_id, role=role))

        if not memory_boost:
            return []

        # Cap to prevent popular-entity fan-out. A30: a hub entity (e.g. a bare
        # "john smith" linking 100+ memories) floods the pool, and a cap by
        # near-uniform hop-boost alone drops the gold BEFORE the query-overlap
        # rerank (below) can see it. Rank the cap by how many of the query's
        # matched entities each memory links to FIRST (a "X's manager" gold
        # links to both the person hub AND the "manager" role → count 2, vs 1
        # for sibling facts about other attributes), with hop-boost as the
        # tiebreak. This collapses the pool to the relevant subset regardless of
        # hub size; the discriminator (e.g. "#0000") is then resolved by
        # _query_overlap after load.
        if len(memory_boost) > GRAPH_MAX_BOOSTED_MEMORIES:
            memory_ids_sorted = sorted(
                memory_boost,
                key=lambda mid: (memory_match_count.get(mid, 0), memory_boost[mid]),
                reverse=True,
            )[:GRAPH_MAX_BOOSTED_MEMORIES]
            memory_boost = {mid: memory_boost[mid] for mid in memory_ids_sorted}

        # H-03: stop here when the pool cannot fill top_k. The caller only takes
        # the exclusive route when it can, and the row build below can never
        # produce more rows than ``memory_boost`` has entries — so an under-sized
        # pool cannot pass that gate whatever the load returns. Loading anyway
        # spent a per-tenant storage permit and pulled full rows (embedding and
        # tsvector included) for a result the caller discards, and worse, left
        # ``_storage_slot_acquired`` set so the scored search that does run
        # skipped the bulkhead. Reporting the pool size lets the caller say why
        # it declined, since ``[]`` alone cannot distinguish "under-filled" from
        # "no linked memories at all" (the ``not memory_boost`` return above).
        #
        # One-directional on purpose. The converse is NOT decidable here: the
        # load applies visibility filters (caller_agent_id / status / valid_at)
        # and rows whose memory did not come back are dropped, so a pool that
        # looks adequate can still return short. That is why the caller re-checks
        # after the load rather than trusting this count.
        if len(memory_boost) < top_k:
            if slot_acquired_marker is not None:
                slot_acquired_marker["_entity_pool_size"] = len(memory_boost)
            return []

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
        # Per-tenant storage bulkhead (CAURA-602 follow-up). C10: when
        # this entity-lookup short-circuit acquires + releases the slot
        # here, we mark the pipeline context so a downstream
        # ``execute_scored_search`` running on the rare fall-through
        # path (entity-lookup matched but produced no filtered rows)
        # doesn't re-acquire and charge the tenant twice for one
        # logical search. Same key as scored-search, intentional —
        # the bucket counts request-level storage pressure, not
        # call-level.
        # Record that the load ran, and against what size of pool. Without this
        # the caller cannot tell "no links at all" from "links existed, the load
        # ran, and visibility filtering dropped every row" — both arrive as an
        # empty list, and reporting the second as the first would send on-call
        # looking for a graph problem when the cause is a permission filter.
        if slot_acquired_marker is not None:
            slot_acquired_marker["_entity_pool_size"] = len(memory_boost)
            slot_acquired_marker["_entity_pool_loaded"] = True
        async with per_tenant_storage_slot("storage_search", tenant_id):
            memories = await sc.load_memories_by_ids(search_data)
        if slot_acquired_marker is not None:
            slot_acquired_marker["_storage_slot_acquired"] = True

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
        # Re-rank the candidate pool by lexical overlap with the query before
        # trimming to top_k. entity_lookup matches greedily and hop-boost is
        # near-uniform, so the exact-entity memory can be diluted below
        # token-sharing siblings; this prefers rows whose content shares more of
        # the query's tokens, with hop-boost as the tiebreak.
        q_tokens = set(extract_entity_tokens(query)) if query else set()

        def _query_overlap(row: types.SimpleNamespace) -> float:
            if not q_tokens:
                return 0.0
            content = getattr(row.Memory, "content", "") or ""
            c_tokens = set(extract_entity_tokens(content))
            return len(q_tokens & c_tokens) / len(q_tokens)

        rows.sort(key=lambda r: (_query_overlap(r), r.score), reverse=True)
        return rows[:top_k]
