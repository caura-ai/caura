"""P3-3: Unbounded fan-out on popular entities — graph boost cap.

Unit tests validate the constant and the capped processing logic.
Integration tests validate that the cap works end-to-end in search.
"""

import uuid
from uuid import UUID

import pytest

from common.embedding import fake_embedding
from core_api.constants import (
    GRAPH_HOP_BOOST,
    GRAPH_MAX_BOOSTED_MEMORIES,
    DEFAULT_SEARCH_TOP_K,
)
from core_api.services.memory_service import search_memories


# ---------------------------------------------------------------------------
# Unit tests: constants and capped logic
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGraphFanoutConstants:
    """Verify the fan-out cap constant is sensible."""

    def test_cap_exists_and_positive(self):
        assert GRAPH_MAX_BOOSTED_MEMORIES > 0

    def test_cap_exceeds_default_search_limit(self):
        """Cap must be larger than the search limit — we need enough
        candidates for the final ranking to choose from."""
        assert GRAPH_MAX_BOOSTED_MEMORIES > DEFAULT_SEARCH_TOP_K

    def test_cap_is_reasonable(self):
        """Cap should be generous enough to avoid cutting off relevant
        results, but bounded enough to prevent the O(N) blowup."""
        assert GRAPH_MAX_BOOSTED_MEMORIES <= 200


@pytest.mark.unit
class TestCappedLinkProcessing:
    """Verify the hop-priority capped processing logic."""

    def _simulate_link_processing(
        self, links: list[tuple[UUID, UUID]], entity_hops: dict[UUID, int]
    ) -> dict[UUID, float]:
        """Replicate the capped link processing logic from search_memories."""
        memory_boost_factor: dict[UUID, float] = {}

        # Sort by hop distance (same as the production code)
        links.sort(key=lambda row: entity_hops[row[1]])

        for mem_id, ent_id in links:
            hop = entity_hops[ent_id]
            boost = GRAPH_HOP_BOOST.get(hop, GRAPH_HOP_BOOST[max(GRAPH_HOP_BOOST)])
            if mem_id not in memory_boost_factor or boost > memory_boost_factor[mem_id]:
                memory_boost_factor[mem_id] = boost
            if len(memory_boost_factor) >= GRAPH_MAX_BOOSTED_MEMORIES:
                break

        return memory_boost_factor

    def test_small_fanout_uncapped(self):
        """Below the cap, all memories get boosted as before."""
        entity_a = uuid.uuid4()
        mems = [uuid.uuid4() for _ in range(5)]
        links = [(m, entity_a) for m in mems]
        hops = {entity_a: 0}

        result = self._simulate_link_processing(links, hops)
        assert len(result) == 5
        for m in mems:
            assert result[m] == GRAPH_HOP_BOOST[0]

    def test_cap_enforced(self):
        """When links exceed the cap, processing stops at the limit."""
        entity_a = uuid.uuid4()
        mems = [uuid.uuid4() for _ in range(GRAPH_MAX_BOOSTED_MEMORIES + 100)]
        links = [(m, entity_a) for m in mems]
        hops = {entity_a: 0}

        result = self._simulate_link_processing(links, hops)
        assert len(result) == GRAPH_MAX_BOOSTED_MEMORIES

    def test_hop_priority_closer_entities_first(self):
        """Memories linked to closer entities fill the cap first."""
        # Entity at hop 0 with exactly cap-count memories
        entity_close = uuid.uuid4()
        close_mems = [uuid.uuid4() for _ in range(GRAPH_MAX_BOOSTED_MEMORIES)]
        close_links = [(m, entity_close) for m in close_mems]

        # Entity at hop 2 with more memories (should be excluded by cap)
        entity_far = uuid.uuid4()
        far_mems = [uuid.uuid4() for _ in range(50)]
        far_links = [(m, entity_far) for m in far_mems]

        hops = {entity_close: 0, entity_far: 2}
        all_links = close_links + far_links

        result = self._simulate_link_processing(all_links, hops)
        assert len(result) == GRAPH_MAX_BOOSTED_MEMORIES
        # All slots taken by close entity — no far entity memories
        for m in close_mems:
            assert m in result
        for m in far_mems:
            assert m not in result

    def test_shared_memory_keeps_best_boost(self):
        """A memory linked to both hop-0 and hop-2 entities gets hop-0 boost."""
        entity_close = uuid.uuid4()
        entity_far = uuid.uuid4()
        shared_mem = uuid.uuid4()

        links = [(shared_mem, entity_far), (shared_mem, entity_close)]
        hops = {entity_close: 0, entity_far: 2}

        result = self._simulate_link_processing(links, hops)
        assert result[shared_mem] == GRAPH_HOP_BOOST[0]

    def test_empty_links(self):
        """No links → no boosted memories."""
        result = self._simulate_link_processing([], {})
        assert result == {}


# ---------------------------------------------------------------------------
# Integration tests: end-to-end via search_memories
# ---------------------------------------------------------------------------


@pytest.fixture
def fanout_tenant():
    """A labelled tenant of this test's own, so no other test's rows can satisfy an
    assertion.

    ``fake_embedding`` is a bag-of-words hash, not a near-orthogonal one: an
    unrelated committed row ("User prefers dark mode in the editor") measures
    cosine 0.33 against a random token — over the 0.3 ``min_similarity`` gate. So
    a unique query token alone does not isolate these tests; the tenant must. The
    ``tenant_id`` fixture is now per-test too, so this adds only the ``fanout``
    label; it is kept because that label is what makes a leaked row identifiable.
    """
    return f"test-tenant-fanout-{uuid.uuid4().hex[:8]}"


@pytest.mark.integration
class TestGraphFanoutIntegration:
    """Verify fan-out cap works in the real search pipeline."""

    async def _create_entity_with_memories(self, sc, tenant_id, fleet_id, entity_name, num_memories):
        """Create an entity and link it to N memories, committed so search sees them.

        Writes through ``sc``, not ``db`` — ``db`` rolls its transaction back at
        teardown, so rows added there are never visible to the separate connection
        ``search_memories`` runs on. That is why these tests carried an xfail
        blaming "fake embeddings produce low similarity": the real cause was
        storage returning 0 rows, and the reason they *xpassed* in a full run was
        that ``len(results) >= 1`` was satisfied by other tests' committed
        leftovers in the shared tenant.

        Both ``entities.search_vector`` and ``memories.search_vector`` are filled
        by triggers that live only in migration 001, so this needs a database
        ``alembic upgrade head`` has run against — ``Base.metadata.create_all``
        alone leaves them NULL and the entity would never match, silently skipping
        the graph boost whose cap is what this class tests. CI migrates first.
        """
        entity = await sc.create_entity({
            "tenant_id": tenant_id,
            "entity_type": "concept",
            "canonical_name": entity_name,
            "fleet_id": fleet_id,
        })
        # Embed the entity name itself, and query by it, so cosine is 1.0 rather
        # than an incidental value — the cap is what's under test, not the gate.
        embedding = fake_embedding(entity_name)
        created = await sc.create_memories([
            {
                "tenant_id": tenant_id,
                "fleet_id": fleet_id,
                "agent_id": "test-agent",
                "memory_type": "fact",
                "content": f"Memory {i} about {entity_name}",
                "embedding": embedding,
                "weight": 0.5,
                "content_hash": f"hash-{entity_name}-{i}",
                "status": "active",
                "client_request_id": str(uuid.uuid4()),
            }
            for i in range(num_memories)
        ])
        memory_ids = [row["id"] for row in created]
        assert all(memory_ids), "bulk insert did not return an id for every memory"
        await sc.bulk_upsert_entity_links([
            {"input_idx": i, "memory_id": mid, "entity_id": entity["id"], "role": "subject"}
            for i, mid in enumerate(memory_ids)
        ])
        return memory_ids

    async def test_small_fanout_all_boosted(self, sc, fanout_tenant, fleet_id):
        """Below the cap, all linked memories are returned."""
        entity_name = f"pythonprog{uuid.uuid4().hex[:8]}"
        memory_ids = await self._create_entity_with_memories(
            sc, fanout_tenant, fleet_id, entity_name, 5,
        )

        results = await search_memories(fanout_tenant, entity_name, fleet_ids=[fleet_id], top_k=10)

        # Assert on this test's own rows, not on a bare count.
        assert {str(r.id) for r in results} == set(memory_ids)

    async def test_large_fanout_capped(self, sc, fanout_tenant, fleet_id):
        """A popular entity with many linked memories doesn't break search."""
        entity_name = f"populartopic{uuid.uuid4().hex[:8]}"
        num_memories = GRAPH_MAX_BOOSTED_MEMORIES + 30
        memory_ids = await self._create_entity_with_memories(
            sc, fanout_tenant, fleet_id, entity_name, num_memories,
        )

        results = await search_memories(fanout_tenant, entity_name, fleet_ids=[fleet_id], top_k=10)

        # top_k is honoured despite the fan-out, and every slot is one of ours.
        assert len(results) == 10
        assert {str(r.id) for r in results} <= set(memory_ids)
