"""P3-2: Relation-type-aware graph expansion.

Unit tests validate the weight map, _relation_weight helper, and the
modified expand_graph return structure.
Integration tests validate that relation types affect boost factors
end-to-end in search.
"""

import uuid

import pytest

from core_api.constants import (
    DEFAULT_RELATION_TYPE_WEIGHT,
    GRAPH_HOP_BOOST,
    RELATION_TYPE_WEIGHTS,
)
from core_api.services.memory_service import _relation_weight


# ---------------------------------------------------------------------------
# Unit tests: constants and helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRelationTypeWeights:
    """Verify the relation type weight map is well-formed."""

    def test_weights_in_valid_range(self):
        """All weights must be between 0 and 1 inclusive."""
        for rel_type, weight in RELATION_TYPE_WEIGHTS.items():
            assert 0.0 <= weight <= 1.0, f"{rel_type} has invalid weight {weight}"

    def test_default_weight_in_valid_range(self):
        assert 0.0 < DEFAULT_RELATION_TYPE_WEIGHT <= 1.0

    def test_strong_relations_higher_than_weak(self):
        """Semantic relations (manages, works_on) should outweigh structural
        ones (located_in, contains)."""
        strong = ("manages", "works_on", "created_by")
        weak = ("located_in", "contains", "instance_of")
        for s in strong:
            for w in weak:
                assert RELATION_TYPE_WEIGHTS[s] > RELATION_TYPE_WEIGHTS[w], (
                    f"{s}({RELATION_TYPE_WEIGHTS[s]}) should be > "
                    f"{w}({RELATION_TYPE_WEIGHTS[w]})"
                )

    def test_known_relation_types_present(self):
        """Types from the LLM extraction prompt must be in the map."""
        expected = {"manages", "works_on", "uses", "belongs_to",
                    "created_by", "depends_on", "located_in"}
        for t in expected:
            assert t in RELATION_TYPE_WEIGHTS, f"Missing relation type: {t}"

    def test_keys_are_lowercase(self):
        for key in RELATION_TYPE_WEIGHTS:
            assert key == key.lower(), f"Key '{key}' should be lowercase"


@pytest.mark.unit
class TestRelationWeightHelper:
    """Verify the _relation_weight computation."""

    def test_known_type_default_row_weight(self):
        """Known type with default DB weight (1.0) returns the type weight."""
        assert _relation_weight("manages", 1.0) == RELATION_TYPE_WEIGHTS["manages"]
        assert _relation_weight("located_in", 1.0) == RELATION_TYPE_WEIGHTS["located_in"]

    def test_unknown_type_gets_default(self):
        """Unknown relation type falls back to DEFAULT_RELATION_TYPE_WEIGHT."""
        assert _relation_weight("invented_by", 1.0) == DEFAULT_RELATION_TYPE_WEIGHT

    def test_row_weight_multiplier(self):
        """DB row weight multiplies the type weight."""
        assert _relation_weight("manages", 0.5) == RELATION_TYPE_WEIGHTS["manages"] * 0.5

    def test_case_insensitive(self):
        """Relation types should match case-insensitively."""
        assert _relation_weight("MANAGES", 1.0) == RELATION_TYPE_WEIGHTS["manages"]
        assert _relation_weight("Located_In", 1.0) == RELATION_TYPE_WEIGHTS["located_in"]

    def test_zero_row_weight(self):
        """Zero DB weight should produce zero effective weight."""
        assert _relation_weight("manages", 0.0) == 0.0


@pytest.mark.unit
class TestExpandGraphReturnStructure:
    """Verify expand_graph return value contract via simulated boost."""

    def _simulate_boost(
        self,
        entity_hops: dict[uuid.UUID, tuple[int, float]],
        links: list[tuple[uuid.UUID, uuid.UUID]],
    ) -> dict[uuid.UUID, float]:
        """Replicate the boost computation from search_memories."""
        memory_boost_factor: dict[uuid.UUID, float] = {}
        links_sorted = sorted(links, key=lambda row: entity_hops[row[1]][0])
        for mem_id, ent_id in links_sorted:
            hop, rel_weight = entity_hops[ent_id]
            hop_boost = GRAPH_HOP_BOOST.get(hop, GRAPH_HOP_BOOST[max(GRAPH_HOP_BOOST)])
            boost = hop_boost * rel_weight
            if mem_id not in memory_boost_factor or boost > memory_boost_factor[mem_id]:
                memory_boost_factor[mem_id] = boost
        return memory_boost_factor

    def test_seed_entity_full_boost(self):
        """Seed entities (hop 0, weight 1.0) get full hop-0 boost."""
        eid = uuid.uuid4()
        mid = uuid.uuid4()
        hops = {eid: (0, 1.0)}
        result = self._simulate_boost(hops, [(mid, eid)])
        assert result[mid] == GRAPH_HOP_BOOST[0] * 1.0

    def test_strong_relation_higher_boost(self):
        """Hop-1 entity via 'manages' (1.0) gets higher boost than via
        'located_in' (0.3) at same hop distance."""
        eid_strong = uuid.uuid4()
        eid_weak = uuid.uuid4()
        mid_strong = uuid.uuid4()
        mid_weak = uuid.uuid4()

        hops = {
            eid_strong: (1, RELATION_TYPE_WEIGHTS["manages"]),
            eid_weak: (1, RELATION_TYPE_WEIGHTS["located_in"]),
        }
        result = self._simulate_boost(hops, [
            (mid_strong, eid_strong),
            (mid_weak, eid_weak),
        ])
        assert result[mid_strong] > result[mid_weak]
        assert result[mid_strong] == GRAPH_HOP_BOOST[1] * RELATION_TYPE_WEIGHTS["manages"]
        assert result[mid_weak] == GRAPH_HOP_BOOST[1] * RELATION_TYPE_WEIGHTS["located_in"]

    def test_weak_hop1_can_be_below_strong_hop2(self):
        """A weak relation at hop 1 can produce a lower boost than a strong
        relation at hop 2, which is the whole point of the fix."""
        eid_weak_h1 = uuid.uuid4()
        eid_strong_h2 = uuid.uuid4()
        mid_weak = uuid.uuid4()
        mid_strong = uuid.uuid4()

        # located_in at hop 1: 1.2 * 0.3 = 0.36
        # manages at hop 2: 1.1 * 1.0 = 1.1
        hops = {
            eid_weak_h1: (1, RELATION_TYPE_WEIGHTS["located_in"]),
            eid_strong_h2: (2, RELATION_TYPE_WEIGHTS["manages"]),
        }
        result = self._simulate_boost(hops, [
            (mid_weak, eid_weak_h1),
            (mid_strong, eid_strong_h2),
        ])
        # Strong relation at hop 2 should outrank weak relation at hop 1
        assert result[mid_strong] > result[mid_weak]

    def test_memory_linked_to_both_keeps_best(self):
        """A memory linked to entities at different hops/weights keeps the
        best boost factor."""
        eid_strong = uuid.uuid4()
        eid_weak = uuid.uuid4()
        shared_mid = uuid.uuid4()

        hops = {
            eid_strong: (0, 1.0),
            eid_weak: (1, RELATION_TYPE_WEIGHTS["located_in"]),
        }
        result = self._simulate_boost(hops, [
            (shared_mid, eid_weak),
            (shared_mid, eid_strong),
        ])
        assert result[shared_mid] == GRAPH_HOP_BOOST[0] * 1.0


# ---------------------------------------------------------------------------
# Integration tests: expand_graph with real DB
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestExpandGraphRelationWeights:
    """Verify expand_graph returns relation weights from the DB."""

    async def _create_entity(self, sc, tenant_id, fleet_id, name):
        entity = await sc.create_entity({
            "tenant_id": tenant_id,
            "fleet_id": fleet_id,
            "entity_type": "concept",
            "canonical_name": name,
        })
        return entity

    async def _create_relation(self, sc, tenant_id, fleet_id, from_id, to_id,
                               relation_type, weight=1.0):
        rel = await sc.create_relation({
            "tenant_id": tenant_id,
            "fleet_id": fleet_id,
            "from_entity_id": str(from_id),
            "to_entity_id": str(to_id),
            "relation_type": relation_type,
            "weight": weight,
        })
        return rel

    async def test_strong_relation_returns_high_weight(self, db, tenant_id, fleet_id):
        from core_api.clients.storage_client import get_storage_client
        from core_api.services.memory_service import expand_graph

        sc = get_storage_client()
        seed = await self._create_entity(sc, tenant_id, fleet_id, "auth team")
        manager = await self._create_entity(sc, tenant_id, fleet_id, "john smith")
        seed_id = uuid.UUID(seed["id"])
        manager_id = uuid.UUID(manager["id"])
        await self._create_relation(
            sc, tenant_id, fleet_id, manager_id, seed_id, "manages",
        )

        result = await expand_graph(db, [seed_id], tenant_id, fleet_id)
        assert seed_id in result
        assert manager_id in result
        hop, weight = result[manager_id]
        assert hop == 1
        assert weight == RELATION_TYPE_WEIGHTS["manages"]

    async def test_weak_relation_returns_low_weight(self, db, tenant_id, fleet_id):
        from core_api.clients.storage_client import get_storage_client
        from core_api.services.memory_service import expand_graph

        sc = get_storage_client()
        team = await self._create_entity(sc, tenant_id, fleet_id, "backend team")
        building = await self._create_entity(sc, tenant_id, fleet_id, "building 4")
        team_id = uuid.UUID(team["id"])
        building_id = uuid.UUID(building["id"])
        await self._create_relation(
            sc, tenant_id, fleet_id, team_id, building_id, "located_in",
        )

        result = await expand_graph(db, [team_id], tenant_id, fleet_id)
        hop, weight = result[building_id]
        assert hop == 1
        assert weight == RELATION_TYPE_WEIGHTS["located_in"]

    async def test_multiple_relations_keeps_strongest(self, db, tenant_id, fleet_id):
        """If two relations connect the same entities, keep the strongest."""
        from core_api.clients.storage_client import get_storage_client
        from core_api.services.memory_service import expand_graph

        sc = get_storage_client()
        team = await self._create_entity(sc, tenant_id, fleet_id, "platform team")
        person = await self._create_entity(sc, tenant_id, fleet_id, "alice")
        team_id = uuid.UUID(team["id"])
        person_id = uuid.UUID(person["id"])
        await self._create_relation(
            sc, tenant_id, fleet_id, person_id, team_id, "located_in",
        )
        await self._create_relation(
            sc, tenant_id, fleet_id, person_id, team_id, "manages",
        )

        result = await expand_graph(db, [team_id], tenant_id, fleet_id)
        _, weight = result[person_id]
        assert weight == RELATION_TYPE_WEIGHTS["manages"]

    async def test_seed_entities_have_weight_1(self, db, tenant_id, fleet_id):
        from core_api.clients.storage_client import get_storage_client
        from core_api.services.memory_service import expand_graph

        sc = get_storage_client()
        seed = await self._create_entity(sc, tenant_id, fleet_id, "seed entity")
        seed_id = uuid.UUID(seed["id"])

        result = await expand_graph(db, [seed_id], tenant_id, fleet_id)
        hop, weight = result[seed_id]
        assert hop == 0
        assert weight == 1.0

    async def test_db_weight_multiplies_type_weight(self, db, tenant_id, fleet_id):
        """Relation.weight column (DB) multiplies the type-based weight."""
        from core_api.clients.storage_client import get_storage_client
        from core_api.services.memory_service import expand_graph

        sc = get_storage_client()
        a = await self._create_entity(sc, tenant_id, fleet_id, "service a")
        b = await self._create_entity(sc, tenant_id, fleet_id, "service b")
        a_id = uuid.UUID(a["id"])
        b_id = uuid.UUID(b["id"])
        await self._create_relation(
            sc, tenant_id, fleet_id, a_id, b_id, "depends_on", weight=0.5,
        )

        result = await expand_graph(db, [a_id], tenant_id, fleet_id)
        _, weight = result[b_id]
        assert weight == pytest.approx(RELATION_TYPE_WEIGHTS["depends_on"] * 0.5)
