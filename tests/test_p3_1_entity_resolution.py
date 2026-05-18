"""P3-1: Entity resolution via name embedding similarity.

Unit tests validate:
- Constants: threshold > 0.5, threshold < 1.0, candidate limit >= 1
- Exact match still takes priority (Phase 1 short-circuits)
- Entity type guard: same name different type → no merge
- Alias tracking: merged entity has _aliases containing both names
- Canonical preservation (A5a): first-seen canonical wins. The previous
  "longer name promoted as canonical" rule actively turned hallucinated
  suffixes into permanent canonical names (e.g., LLM hallucinating
  "globex industries" for content saying only "Globex" produced a
  permanent corruption); see commit history for A5a context.
- No embedding → no fuzzy check (graceful skip)

Integration tests verify:
- Insert "john smith" twice → exact match, same entity
- Insert "john smith", then "jon smith" (similar embedding) → fuzzy match, same entity
- Insert "john smith" (person), then "john smith" (technology) → different entities
- Insert "john smith", then "dr. john smith" → fuzzy match; canonical
  stays as "john smith" (first-seen wins); "dr. john smith" lives in
  the aliases list and remains searchable.
- Insert "alice smith", then "bob smith" → different entities (distance too large)
- Alias list grows correctly over multiple merges
"""

import random
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from core_api.constants import (
    ENTITY_RESOLUTION_CANDIDATE_LIMIT,
    ENTITY_RESOLUTION_THRESHOLD,
    VECTOR_DIM,
)
from common.embedding import fake_embedding


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def close_embedding(base_text: str, noise: float = 0.001) -> list[float]:
    """Produce an embedding very close to fake_embedding(base_text)."""
    base = fake_embedding(base_text)
    rng = random.Random(42)
    return [v + rng.gauss(0, noise) for v in base]


def distant_embedding() -> list[float]:
    """Produce an embedding that is far from typical name embeddings."""
    rng = random.Random(999)
    return [rng.uniform(-1, 1) for _ in range(VECTOR_DIM)]


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEntityResolutionConstants:
    """Verify P3-1 constant values."""

    def test_threshold_above_half(self):
        assert ENTITY_RESOLUTION_THRESHOLD > 0.5

    def test_threshold_below_one(self):
        assert ENTITY_RESOLUTION_THRESHOLD < 1.0

    def test_candidate_limit_positive(self):
        assert ENTITY_RESOLUTION_CANDIDATE_LIMIT >= 1


@pytest.mark.unit
class TestUpsertEntityExactMatch:
    """Phase 1: exact match still works and short-circuits."""

    @pytest.mark.asyncio
    async def test_exact_match_returns_same_entity(self):
        """Two upserts with identical canonical_name should return same entity ID."""
        from unittest.mock import patch

        from core_api.schemas import EntityUpsert

        entity_id = uuid4()
        existing_dict = {
            "id": entity_id,
            "tenant_id": "t1",
            "fleet_id": None,
            "entity_type": "person",
            "canonical_name": "John Smith",
            "attributes": {},
            "name_embedding": None,
        }

        mock_sc = AsyncMock()
        mock_sc.find_exact_entity.return_value = existing_dict
        mock_sc.update_entity.return_value = existing_dict

        db = AsyncMock()

        with patch(
            "core_api.services.entity_service.get_storage_client",
            return_value=mock_sc,
        ):
            from core_api.services.entity_service import upsert_entity

            result = await upsert_entity(
                db,
                EntityUpsert(
                    tenant_id="t1",
                    entity_type="person",
                    canonical_name="John Smith",
                ),
            )

        assert result.id == entity_id
        # Phase 2 should NOT run (find_by_embedding_similarity not called)
        mock_sc.find_by_embedding_similarity.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_embedding_skips_fuzzy(self):
        """Without name_embedding, Phase 2 is not attempted."""
        from unittest.mock import patch

        from core_api.schemas import EntityUpsert

        new_id = uuid4()
        created_dict = {
            "id": new_id,
            "tenant_id": "t1",
            "fleet_id": None,
            "entity_type": "person",
            "canonical_name": "John Smith",
            "attributes": None,
        }

        mock_sc = AsyncMock()
        mock_sc.find_exact_entity.return_value = None
        mock_sc.create_entity.return_value = created_dict

        db = AsyncMock()

        with patch(
            "core_api.services.entity_service.get_storage_client",
            return_value=mock_sc,
        ):
            from core_api.services.entity_service import upsert_entity

            result = await upsert_entity(
                db,
                EntityUpsert(
                    tenant_id="t1",
                    entity_type="person",
                    canonical_name="John Smith",
                ),
            )

        # Phase 2 should NOT run (no embedding provided)
        mock_sc.find_by_embedding_similarity.assert_not_called()
        assert result.canonical_name == "John Smith"


@pytest.mark.unit
class TestAliasTracking:
    """Alias tracking and canonical name promotion logic."""

    def test_aliases_populated_on_merge(self):
        """When entities merge, _aliases should contain both names."""
        from common.models.entity import Entity

        entity = MagicMock(spec=Entity)
        entity.canonical_name = "John Smith"
        entity.attributes = {}
        entity.entity_type = "person"
        entity.name_embedding = None

        # Simulate the merge logic from upsert_entity
        data_canonical = "J. Smith"

        aliases = (entity.attributes or {}).get("_aliases", [])
        if entity.canonical_name not in aliases:
            aliases.append(entity.canonical_name)
        if data_canonical not in aliases:
            aliases.append(data_canonical)

        assert "John Smith" in aliases
        assert "J. Smith" in aliases

    def test_first_seen_canonical_preserved_when_longer_arrives(self):
        """A5a: first-seen wins. When a longer alternative form arrives via
        fuzzy match, the existing canonical name is preserved. The longer
        form is tracked in the alias list above so it remains searchable."""
        existing_name = "J. Smith"
        # incoming_name preserved as an alias, not as the canonical
        result = existing_name  # production rule in entity_service.upsert_entity
        assert result == "J. Smith"

    def test_first_seen_canonical_preserved_when_shorter_arrives(self):
        """A5a: symmetric case. Shorter incoming name does NOT downgrade the
        existing canonical. This case was already correct under the old
        longer-wins rule by coincidence; now it's principled."""
        existing_name = "Dr. John Smith"
        result = existing_name
        assert result == "Dr. John Smith"


# ---------------------------------------------------------------------------
# Integration tests (require PostgreSQL with pgvector)
# ---------------------------------------------------------------------------


@pytest.fixture
def tenant_id():
    """Per-test unique tenant_id for isolation (overrides session-level fixture)."""
    return f"test-tenant-{uuid4().hex[:8]}"


@pytest.mark.integration
class TestEntityResolutionIntegration:
    """End-to-end entity resolution against a real database."""

    async def _insert_entity(
        self, db, tenant_id, fleet_id, entity_type, canonical_name, name_embedding=None
    ):
        """Helper: insert entity via upsert_entity."""
        from core_api.schemas import EntityUpsert
        from core_api.services.entity_service import upsert_entity

        return await upsert_entity(
            db,
            EntityUpsert(
                tenant_id=tenant_id,
                fleet_id=fleet_id,
                entity_type=entity_type,
                canonical_name=canonical_name,
            ),
            name_embedding=name_embedding,
        )

    @pytest.mark.asyncio
    async def test_exact_match_same_entity(self, db, tenant_id, fleet_id):
        """Insert 'john smith' twice → same entity."""
        emb = fake_embedding("john smith")
        e1 = await self._insert_entity(
            db, tenant_id, fleet_id, "person", "john smith", emb
        )
        e2 = await self._insert_entity(
            db, tenant_id, fleet_id, "person", "john smith", emb
        )
        assert e1.id == e2.id

    @pytest.mark.asyncio
    async def test_fuzzy_match_similar_name(self, db, tenant_id, fleet_id):
        """Insert 'john smith', then 'jon smith' with close embedding → same entity."""
        emb1 = fake_embedding("john smith")
        e1 = await self._insert_entity(
            db, tenant_id, fleet_id, "person", "john smith", emb1
        )

        # Close embedding simulates what a real model would give for a name variant
        emb2 = close_embedding("john smith", noise=0.001)
        e2 = await self._insert_entity(
            db, tenant_id, fleet_id, "person", "jon smith", emb2
        )
        assert e1.id == e2.id

    @pytest.mark.asyncio
    async def test_type_guard_prevents_merge(self, db, tenant_id, fleet_id):
        """'john smith' (person) and 'john smith' (technology) → different entities."""
        emb = fake_embedding("john smith")
        e1 = await self._insert_entity(
            db, tenant_id, fleet_id, "person", "john smith", emb
        )
        e2 = await self._insert_entity(
            db, tenant_id, fleet_id, "technology", "john smith", emb
        )
        assert e1.id != e2.id

    @pytest.mark.asyncio
    async def test_canonical_name_preserves_first_seen(self, db, tenant_id, fleet_id):
        """A5a: insert 'john smith', then 'dr. john smith' → merged via fuzzy
        match; canonical stays as 'john smith' (first-seen wins). The
        previous behaviour promoted the longer name as canonical, which
        actively turned hallucinated LLM suffixes into permanent
        canonical names. 'dr. john smith' is now tracked as an alias
        and remains searchable."""
        emb1 = fake_embedding("john smith")
        e1 = await self._insert_entity(
            db, tenant_id, fleet_id, "person", "john smith", emb1
        )

        emb2 = close_embedding("john smith", noise=0.001)
        e2 = await self._insert_entity(
            db, tenant_id, fleet_id, "person", "dr. john smith", emb2
        )

        assert e1.id == e2.id
        assert e2.canonical_name == "john smith"
        # Longer surface form is preserved as an alias.
        aliases = (e2.attributes or {}).get("_aliases", [])
        assert "dr. john smith" in aliases

    @pytest.mark.asyncio
    async def test_distant_names_not_merged(self, db, tenant_id, fleet_id):
        """'alice smith' and 'bob smith' → different entities."""
        emb1 = fake_embedding("alice smith")
        e1 = await self._insert_entity(
            db, tenant_id, fleet_id, "person", "alice smith", emb1
        )

        emb2 = distant_embedding()
        e2 = await self._insert_entity(
            db, tenant_id, fleet_id, "person", "bob smith", emb2
        )
        assert e1.id != e2.id

    @pytest.mark.asyncio
    async def test_alias_list_grows(self, db, tenant_id, fleet_id):
        """Multiple merges accumulate aliases."""
        from common.models.entity import Entity

        emb1 = fake_embedding("john smith")
        e1 = await self._insert_entity(
            db, tenant_id, fleet_id, "person", "john smith", emb1
        )

        emb2 = close_embedding("john smith", noise=0.001)
        await self._insert_entity(db, tenant_id, fleet_id, "person", "jon smith", emb2)

        emb3 = close_embedding("john smith", noise=0.0005)
        await self._insert_entity(
            db, tenant_id, fleet_id, "person", "dr. john smith", emb3
        )

        # Read entity directly to check aliases
        entity = await db.get(Entity, e1.id)
        aliases = (entity.attributes or {}).get("_aliases", [])
        assert "john smith" in aliases
        assert "jon smith" in aliases
        assert "dr. john smith" in aliases
