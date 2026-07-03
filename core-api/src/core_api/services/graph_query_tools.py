"""Typed graph-query tool wrappers for the opt-in agentic reasoning loop (RL-01).

Each tool wraps an already-tested storage-client/service primitive so
``graph_reasoning_loop.py`` can accumulate a boosted-memory set turn by turn
without re-deriving retrieval logic that already exists in
``_entity_boost_via_storage`` (parallel_embed_entity_boost.py) and
``memory_service``. All tools return ``(memory_ids, entity_ids, note)``.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from core_api.clients.storage_client import get_storage_client
from core_api.constants import GRAPH_MAX_BOOSTED_MEMORIES, GRAPH_MAX_EXPANDED_ENTITIES
from core_api.services.entity_tokens import extract_entity_tokens
from core_api.services.memory_service import _extract_temporal_date_range, expand_graph

GRAPH_QUERY_TOOL_NAMES = [
    "query_by_keyword",
    "query_by_graph_context",
    "query_by_time_range",
    "query_by_entity_links",
]

TOOL_SPECS = {
    "query_by_keyword": (
        "Entity full-text search by keyword — good first step for any query naming a "
        "person, place, or thing."
    ),
    "query_by_graph_context": (
        "Expand already-found entities across the knowledge graph (relationships, "
        "≤2 hops) — good for 'X's role with Y' style questions."
    ),
    "query_by_time_range": (
        "List memories in a parsed date range — good for 'three weeks ago' / "
        "'last month' style questions."
    ),
    "query_by_entity_links": (
        "Fetch memories directly linked to already-found entities without further "
        "graph expansion."
    ),
}


async def _memory_ids_for_entities(entity_ids: set[UUID]) -> set[UUID]:
    if not entity_ids:
        return set()
    sc = get_storage_client()
    raw_links = await sc.get_memory_ids_by_entity_ids([str(eid) for eid in entity_ids])
    return {UUID(link["memory_id"]) for link in raw_links}


async def query_by_keyword(
    query: str,
    tenant_id: str,
    fleet_ids: list[str] | None = None,
) -> tuple[set[UUID], set[UUID], str]:
    """Entity FTS lookup by keyword, then memories linked to the matched entities."""
    tokens = extract_entity_tokens(query)
    if not tokens:
        return set(), set(), "no entity-fts tokens extracted from query"

    sc = get_storage_client()
    fts_data: dict = {"tokens": tokens, "tenant_id": tenant_id}
    if fleet_ids:
        fts_data["fleet_ids"] = fleet_ids
    matched_id_strs = await sc.fts_search_entities(fts_data)
    entity_ids = {UUID(eid) for eid in matched_id_strs}
    if not entity_ids:
        return set(), set(), f"no entities matched tokens {tokens}"

    memory_ids = await _memory_ids_for_entities(entity_ids)
    return memory_ids, entity_ids, f"matched {len(entity_ids)} entities via keyword FTS"


async def query_by_graph_context(
    seed_entity_ids: list[UUID],
    tenant_id: str,
    fleet_ids: list[str] | None = None,
    max_hops: int = 2,
) -> tuple[set[UUID], set[UUID], str]:
    """Expand a seed entity set across the knowledge graph and collect linked memories."""
    if not seed_entity_ids:
        return set(), set(), "no seed entities provided"

    entity_hops = await expand_graph(
        seed_entity_ids,
        tenant_id,
        fleet_ids[0] if fleet_ids and len(fleet_ids) == 1 else None,
        max_hops=max_hops,
    )
    if not entity_hops:
        return set(), set(), "graph expansion returned no linked entities"

    all_entity_ids = list(entity_hops.keys())
    if len(all_entity_ids) > GRAPH_MAX_EXPANDED_ENTITIES:
        all_entity_ids = sorted(
            all_entity_ids,
            key=lambda eid: (entity_hops[eid][0], -entity_hops[eid][1]),
        )[:GRAPH_MAX_EXPANDED_ENTITIES]

    entity_id_set = set(all_entity_ids)
    memory_ids = await _memory_ids_for_entities(entity_id_set)
    return (
        memory_ids,
        entity_id_set,
        f"expanded {len(seed_entity_ids)} seed entities to {len(entity_id_set)} entities ({max_hops} hops)",
    )


async def query_by_time_range(
    query: str,
    tenant_id: str,
    fleet_ids: list[str] | None = None,
    reference_datetime: datetime | None = None,
) -> tuple[set[UUID], set[UUID], str]:
    """Parse a temporal expression from *query* and list memories in that date range."""
    date_range = _extract_temporal_date_range(query, reference_datetime)
    if date_range is None:
        return set(), set(), "no temporal expression detected in query"

    sc = get_storage_client()
    list_payload = {
        "tenant_id": tenant_id,
        "fleet_id": fleet_ids[0] if fleet_ids and len(fleet_ids) == 1 else None,
        "created_after": date_range["start_date"],
        "created_before": date_range["end_date"],
        "include_deleted": False,
        "sort": "created_at",
        "order": "desc",
        "limit": GRAPH_MAX_BOOSTED_MEMORIES,
    }
    rows = await sc.list_memories_by_filters(list_payload)
    memory_ids = {UUID(row["id"]) for row in rows}
    return (
        memory_ids,
        set(),
        f"found {len(memory_ids)} memories in range {date_range['start_date']}..{date_range['end_date']}",
    )


async def query_by_entity_links(
    entity_ids: list[UUID],
) -> tuple[set[UUID], set[UUID], str]:
    """Collect memories directly linked to a known set of entity ids."""
    if not entity_ids:
        return set(), set(), "no entity ids provided"

    entity_id_set = set(entity_ids)
    memory_ids = await _memory_ids_for_entities(entity_id_set)
    return memory_ids, entity_id_set, f"found {len(memory_ids)} memories linked to {len(entity_id_set)} entities"
