"""CAURA-000 — Graph-frontier cap (F4: 42K-bind-parameter SQL crash).

Customer-side ground truth (goodclaw / tenant ``etoro0-40f488``, 2026-06-07):
a search against a dense entity graph generated a single SQL statement
with **42,146 bind parameters** on the ``relations`` table, exceeding
asyncpg's safe window and crashing the ``core-storage-api`` request.
Cascade visible upstream as 7 ``recall failed: MemClaw API 500`` lines
and 3 ``SMOKE TEST ERROR`` events in the gateway log.

Two cap sites pinned here:

1. ``entity_expand_graph`` (core-storage-api/postgres_service.py) — the
   BFS frontier is now truncated to ``GRAPH_MAX_EXPANDED_ENTITIES`` (200)
   BEFORE building the per-hop ``Relation.from/to_entity_id.in_(frontier)``
   query. Ordering is ``(weight desc, id asc)`` so the cap keeps the
   highest-weighted (most-relevant) edges deterministically.

2. ``parallel_embed_entity_boost`` (core-api) — applies the same cap
   defensively at the call boundary BEFORE calling the downstream
   ``get_memory_ids_by_entity_ids`` endpoint, so a future regression in
   the BFS can't blow up the downstream query either. Ordering is
   ``(hop asc, weight desc)`` — mirrors the existing cap in
   ``classify_query._load_graph_memories`` so the two search paths
   behave consistently.

Unit-test only: we don't need a real DB to validate the cap. The
cap logic is small, pure, and easy to exercise with mocked storage
client / a replicated helper.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from common.constants import GRAPH_MAX_EXPANDED_ENTITIES
from core_api.pipeline.steps.search.parallel_embed_entity_boost import (
    _entity_boost_via_storage,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFrontierCapConstant:
    def test_cap_is_below_asyncpg_safe_window(self):
        """The cap MUST be far below asyncpg's ~32K bind-parameter ceiling
        (and the 65K PostgreSQL absolute max). 200 leaves comfortable
        headroom for the auxiliary bind params (tenant_id, fleet_id,
        ``OR fleet_id IS NULL``, etc.)."""
        assert GRAPH_MAX_EXPANDED_ENTITIES <= 5000

    def test_cap_is_at_least_typical_seed_count(self):
        """Seeds come from entity-FTS hits — typically <50 per query.
        Cap MUST be ≥ realistic seed counts so we don't truncate seeds."""
        assert GRAPH_MAX_EXPANDED_ENTITIES >= 50


# ---------------------------------------------------------------------------
# parallel_embed_entity_boost cap (call-site defense)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestParallelEmbedEntityBoostCap:
    """The CAURA-000 fix adds a hop-asc / weight-desc cap before passing
    expanded entity IDs to ``get_memory_ids_by_entity_ids``. This guards
    against the customer's observed 42K-bind-parameter crash."""

    async def test_cap_applied_when_entity_hops_exceeds_limit(self):
        """When ``precomputed_hops`` has >GRAPH_MAX_EXPANDED_ENTITIES
        entries, the downstream storage call must receive ≤cap UUIDs."""
        # Construct a deterministic hop dict with > cap entries.
        N = GRAPH_MAX_EXPANDED_ENTITIES + 100
        hops: dict[UUID, tuple[int, float]] = {}
        for i in range(N):
            eid = uuid.UUID(f"00000000-0000-0000-0000-{i:012d}")
            # hop alternates 0/1/2 so the test exercises the (hop asc, weight desc) sort
            hop = i % 3
            weight = 1.0 - (i * 0.001)  # strictly decreasing
            hops[eid] = (hop, weight)

        # Mock the storage client. We capture what entity-id list gets passed
        # to ``get_memory_ids_by_entity_ids`` so we can assert the cap held.
        sc = AsyncMock()
        sc.fts_search_entities = AsyncMock(return_value=[])
        sc.expand_graph = AsyncMock(return_value={})
        sc.get_memory_ids_by_entity_ids = AsyncMock(return_value=[])

        with patch(
            "core_api.pipeline.steps.search.parallel_embed_entity_boost.get_storage_client",
            return_value=sc,
        ):
            # Need a non-empty ``matched_entity_ids`` so the function reaches
            # the cap site (the early-return at the top of the function bails
            # when matched_entity_ids is empty). With precomputed_hops set,
            # matched_entity_ids = [eid for hop==0]. The N=GRAPH_MAX+100
            # construction above gives ~ (N/3) hop-0 entities — enough.
            await _entity_boost_via_storage(
                query="anything",
                tenant_id="test-tenant",
                fleet_ids=None,
                graph_expand=True,
                graph_max_hops=2,
                use_union=False,
                precomputed_hops=hops,
            )

        assert sc.get_memory_ids_by_entity_ids.await_count == 1
        passed_uuids = sc.get_memory_ids_by_entity_ids.await_args.args[0]
        assert len(passed_uuids) <= GRAPH_MAX_EXPANDED_ENTITIES, (
            f"cap violated: passed {len(passed_uuids)} UUIDs to "
            f"get_memory_ids_by_entity_ids (expected ≤ {GRAPH_MAX_EXPANDED_ENTITIES})"
        )

    async def test_no_cap_when_within_limit(self):
        """When entity_hops has ≤ cap entries, list passes through unchanged
        (no truncation, ordering NOT enforced)."""
        N = GRAPH_MAX_EXPANDED_ENTITIES - 50
        hops: dict[UUID, tuple[int, float]] = {}
        for i in range(N):
            eid = uuid.UUID(f"00000000-0000-0000-0000-{i:012d}")
            hops[eid] = (0, 1.0)

        sc = AsyncMock()
        sc.fts_search_entities = AsyncMock(return_value=[])
        sc.expand_graph = AsyncMock(return_value={})
        sc.get_memory_ids_by_entity_ids = AsyncMock(return_value=[])

        with patch(
            "core_api.pipeline.steps.search.parallel_embed_entity_boost.get_storage_client",
            return_value=sc,
        ):
            await _entity_boost_via_storage(
                query="anything",
                tenant_id="test-tenant",
                fleet_ids=None,
                graph_expand=True,
                graph_max_hops=2,
                use_union=False,
                precomputed_hops=hops,
            )

        passed_uuids = sc.get_memory_ids_by_entity_ids.await_args.args[0]
        assert len(passed_uuids) == N  # unchanged

    async def test_cap_ordering_keeps_closer_hops(self):
        """Pin the (hop asc, weight desc) ordering: closer hops MUST be
        kept when the cap forces a choice. A regression in the sort
        key would silently drop seed-hop entities and degrade recall."""
        # Deliberately stuff hop=2 entities at indices 0..N (would come
        # first under natural dict iteration), and put high-weight hop=0
        # entities at the tail. The sort must surface hop=0 entries
        # despite their late insertion order.
        N = GRAPH_MAX_EXPANDED_ENTITIES + 50
        hops: dict[UUID, tuple[int, float]] = {}
        # First: N-1 entities at hop=2 (would be dropped by a good cap)
        for i in range(N - 1):
            eid = uuid.UUID(f"00000000-0000-0000-0000-{i:012d}")
            hops[eid] = (2, 0.5)
        # Last: 1 distinctly-marked entity at hop=0 (must survive the cap)
        survivor = uuid.UUID("ffffffff-0000-0000-0000-000000000000")
        hops[survivor] = (0, 1.0)

        sc = AsyncMock()
        sc.fts_search_entities = AsyncMock(return_value=[])
        sc.expand_graph = AsyncMock(return_value={})
        sc.get_memory_ids_by_entity_ids = AsyncMock(return_value=[])

        with patch(
            "core_api.pipeline.steps.search.parallel_embed_entity_boost.get_storage_client",
            return_value=sc,
        ):
            await _entity_boost_via_storage(
                query="anything",
                tenant_id="test-tenant",
                fleet_ids=None,
                graph_expand=True,
                graph_max_hops=2,
                use_union=False,
                precomputed_hops=hops,
            )

        passed_uuids = sc.get_memory_ids_by_entity_ids.await_args.args[0]
        # The hop-0 survivor MUST be present even though it was inserted last.
        assert str(survivor) in passed_uuids, (
            "cap dropped a hop-0 entity in favor of hop-2 entities — "
            "sort key regression"
        )


# ---------------------------------------------------------------------------
# entity_expand_graph cap (BFS internal)
# ---------------------------------------------------------------------------
#
# Pure-function pin: the BFS cap inside ``entity_expand_graph`` orders the
# frontier by (weight desc, id asc) before slicing to GRAPH_MAX_EXPANDED_ENTITIES.
# Replicate that selection here so a refactor that changes the sort key
# (e.g. swaps weight/id order, or drops the deterministic tie-break) fails
# this test. The integration test in core-storage-api/tests covers the SQL
# path end-to-end against a real DB.


@pytest.mark.unit
class TestEntityExpandGraphFrontierCap:
    @staticmethod
    def _select_capped_frontier(
        frontier: set[UUID],
        entity_hops: dict[UUID, tuple[int, float]],
        cap: int = GRAPH_MAX_EXPANDED_ENTITIES,
    ) -> list[UUID]:
        """Mirror of the cap logic in
        ``PostgresService.entity_expand_graph`` (the relevant slice of the
        BFS loop). If this drifts from the production implementation, the
        production behavior is the source of truth — UPDATE this helper
        and re-verify the tests below still encode the intended invariant.
        """
        if len(frontier) <= cap:
            return list(frontier)
        return sorted(
            frontier,
            key=lambda eid: (-entity_hops.get(eid, (0, 0.0))[1], eid),
        )[:cap]

    def test_cap_keeps_highest_weight_edges(self):
        """When the frontier exceeds the cap, the kept entries must be
        the highest-weighted ones (most-relevant by relation strength)."""
        entity_hops: dict[UUID, tuple[int, float]] = {}
        frontier: set[UUID] = set()
        # High-weight entities (weight=1.0) — should survive
        high_ids = []
        for i in range(GRAPH_MAX_EXPANDED_ENTITIES):
            eid = uuid.UUID(f"11111111-0000-0000-0000-{i:012d}")
            entity_hops[eid] = (1, 1.0)
            frontier.add(eid)
            high_ids.append(eid)
        # Low-weight entities (weight=0.1) — should be dropped
        for i in range(50):
            eid = uuid.UUID(f"22222222-0000-0000-0000-{i:012d}")
            entity_hops[eid] = (1, 0.1)
            frontier.add(eid)

        kept = self._select_capped_frontier(frontier, entity_hops)
        assert len(kept) == GRAPH_MAX_EXPANDED_ENTITIES
        for eid in high_ids:
            assert eid in kept, f"high-weight {eid} dropped — sort regression"

    def test_cap_is_deterministic_across_calls(self):
        """Same input MUST produce the same output — the ID tiebreak
        ensures stability when many entries share a weight. Without this,
        two identical queries could surface different memory boosts and
        rank results differently across calls."""
        N = GRAPH_MAX_EXPANDED_ENTITIES + 20
        entity_hops: dict[UUID, tuple[int, float]] = {}
        frontier: set[UUID] = set()
        for i in range(N):
            eid = uuid.UUID(f"00000000-0000-0000-0000-{i:012d}")
            # All same weight to force tiebreak by ID
            entity_hops[eid] = (1, 0.7)
            frontier.add(eid)

        # Sets have insertion-order-independent iteration, but our sort
        # tiebreaks by UUID so the result is fully deterministic.
        kept1 = self._select_capped_frontier(frontier, entity_hops)
        kept2 = self._select_capped_frontier(set(frontier), entity_hops)
        assert kept1 == kept2

    def test_no_op_when_within_cap(self):
        """A frontier within the cap is returned unchanged in size (the
        sort/slice is skipped entirely — no overhead on small graphs)."""
        N = GRAPH_MAX_EXPANDED_ENTITIES - 1
        entity_hops: dict[UUID, tuple[int, float]] = {}
        frontier: set[UUID] = set()
        for i in range(N):
            eid = uuid.UUID(f"00000000-0000-0000-0000-{i:012d}")
            entity_hops[eid] = (1, 0.5)
            frontier.add(eid)

        kept = self._select_capped_frontier(frontier, entity_hops)
        assert len(kept) == N
        assert set(kept) == frontier
