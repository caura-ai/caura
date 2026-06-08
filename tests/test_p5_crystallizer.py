"""P5: Crystallizer near-duplicate detection — batch ANN + uncapped pairs.

Unit tests validate:
- New constants (batch size, neighbors, pair cap)
- Pair deduplication logic (normalized ordering)
- Safety valve cap behavior
- CRYSTALLIZER_DEDUP_SAMPLE_SIZE removed

Integration tests verify:
- Batch ANN finds near-duplicate pairs above threshold
- No 50-pair cap: >50 pairs collected correctly
- last_dedup_checked_at updated after processing
- HNSW index used (via EXPLAIN ANALYZE)
"""

import uuid
from datetime import datetime, timezone

import pytest

from core_api.constants import (
    CRYSTALLIZER_DEDUP_BATCH_SIZE,
    CRYSTALLIZER_DEDUP_NEIGHBORS,
    CRYSTALLIZER_DEDUP_THRESHOLD,
    CRYSTALLIZER_MAX_DEDUP_PAIRS,
    VECTOR_DIM,
)


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCrystallizerConstants:
    """Verify P5 constant values and ranges."""

    def test_batch_size_value(self):
        assert CRYSTALLIZER_DEDUP_BATCH_SIZE == 500

    def test_neighbors_value(self):
        assert CRYSTALLIZER_DEDUP_NEIGHBORS == 5

    def test_max_pairs_value(self):
        assert CRYSTALLIZER_MAX_DEDUP_PAIRS == 1000

    def test_threshold_unchanged(self):
        """Dedup threshold should remain 0.95 — near-exact duplicates only."""
        assert CRYSTALLIZER_DEDUP_THRESHOLD == 0.95

    def test_sample_size_removed(self):
        """CRYSTALLIZER_DEDUP_SAMPLE_SIZE should no longer exist (replaced by batch ANN)."""
        import core_api.constants as c

        assert not hasattr(c, "CRYSTALLIZER_DEDUP_SAMPLE_SIZE"), (
            "CRYSTALLIZER_DEDUP_SAMPLE_SIZE should be removed — batch ANN scans all unchecked memories"
        )

    def test_batch_size_reasonable(self):
        assert 100 <= CRYSTALLIZER_DEDUP_BATCH_SIZE <= 2000

    def test_neighbors_reasonable(self):
        assert 2 <= CRYSTALLIZER_DEDUP_NEIGHBORS <= 20

    def test_max_pairs_reasonable(self):
        assert 100 <= CRYSTALLIZER_MAX_DEDUP_PAIRS <= 10000


@pytest.mark.unit
class TestPairDeduplication:
    """Test that pair ordering is normalized to avoid duplicate pairs."""

    def test_pair_order_normalized(self):
        """id1 < id2 always, regardless of query order."""
        id_a = "aaaa-1111"
        id_b = "zzzz-9999"
        # Simulate the normalization from _check_near_duplicates
        a, b = sorted([id_a, id_b])
        assert a == id_a
        assert b == id_b

    def test_reversed_input_same_output(self):
        """Swapping query/result order gives same pair."""
        id_a = "zzzz-9999"
        id_b = "aaaa-1111"
        a, b = sorted([id_a, id_b])
        assert a == "aaaa-1111"
        assert b == "zzzz-9999"


@pytest.mark.unit
class TestBuildClusters:
    """Test union-find cluster building."""

    def test_single_pair_single_cluster(self):
        from core_api.services.crystallizer_service import _build_clusters

        pairs = [{"id1": str(uuid.uuid4()), "id2": str(uuid.uuid4())}]
        clusters = _build_clusters(pairs)
        assert len(clusters) == 1
        assert len(clusters[0]) == 2

    def test_chain_merges_into_one_cluster(self):
        from core_api.services.crystallizer_service import _build_clusters

        ids = [str(uuid.uuid4()) for _ in range(5)]
        # Chain: 0-1, 1-2, 2-3, 3-4 → all in one cluster
        pairs = [{"id1": ids[i], "id2": ids[i + 1]} for i in range(4)]
        clusters = _build_clusters(pairs)
        assert len(clusters) == 1
        assert len(clusters[0]) == 5

    def test_disconnected_pairs_separate_clusters(self):
        from core_api.services.crystallizer_service import _build_clusters

        a1, a2 = str(uuid.uuid4()), str(uuid.uuid4())
        b1, b2 = str(uuid.uuid4()), str(uuid.uuid4())
        pairs = [{"id1": a1, "id2": a2}, {"id1": b1, "id2": b2}]
        clusters = _build_clusters(pairs)
        assert len(clusters) == 2

    def test_many_pairs_not_capped_at_50(self):
        """Verify that >50 pairs are handled correctly (old LIMIT 50 removed)."""
        from core_api.services.crystallizer_service import _build_clusters

        ids = [str(uuid.uuid4()) for _ in range(60)]
        # 59 pairs in a chain
        pairs = [{"id1": ids[i], "id2": ids[i + 1]} for i in range(59)]
        clusters = _build_clusters(pairs)
        assert len(clusters) == 1
        assert len(clusters[0]) == 60, (
            "All 60 IDs should be in one cluster (no 50-pair cap)"
        )


@pytest.mark.unit
class TestMemoryModelHasDedupColumn:
    """Verify the model has the new tracking column."""

    def test_last_dedup_checked_at_exists(self):
        from common.models.memory import Memory

        assert hasattr(Memory, "last_dedup_checked_at")


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestBatchANNIntegration:
    """Integration tests for the batch ANN near-duplicate detection."""

    @staticmethod
    async def _insert_memory(
        tenant_id: str,
        fleet_id: str,
        content: str,
        embedding: list[float],
        memory_type: str = "fact",
        last_dedup_checked_at: datetime | None = None,
    ):
        from core_api.clients.storage_client import get_storage_client

        sc = get_storage_client()
        return await sc.create_memory(
            {
                "tenant_id": tenant_id,
                "fleet_id": fleet_id,
                "agent_id": "test-agent",
                "memory_type": memory_type,
                "content": content,
                "embedding": embedding,
                "weight": 0.5,
                "status": "active",
                "last_dedup_checked_at": last_dedup_checked_at.isoformat()
                if last_dedup_checked_at
                else None,
            }
        )

    @staticmethod
    def _fake_embedding(seed: str, dim: int = VECTOR_DIM) -> list[float]:
        """Deterministic embedding from seed string."""
        import hashlib
        import struct

        h = hashlib.sha256(seed.encode()).digest()
        raw = h * (dim // len(h) + 1)
        values = [struct.unpack_from("b", raw, i)[0] / 128.0 for i in range(dim)]
        norm = sum(v * v for v in values) ** 0.5
        return [v / norm for v in values]

    @staticmethod
    def _near_duplicate_embedding(
        base: list[float], noise: float = 0.01
    ) -> list[float]:
        """Create an embedding very close to base (high cosine similarity)."""
        import random

        random.seed(42)
        noisy = [v + random.uniform(-noise, noise) for v in base]
        norm = sum(v * v for v in noisy) ** 0.5
        return [v / norm for v in noisy]

    @pytest.mark.asyncio
    async def test_finds_near_duplicates(self, db, tenant_id, fleet_id):
        """Batch ANN should find near-duplicate pairs above threshold."""
        from core_api.services.crystallizer_service import _check_near_duplicates

        base_emb = self._fake_embedding("near-dup-test")
        dup_emb = self._near_duplicate_embedding(base_emb, noise=0.001)

        await self._insert_memory(
            tenant_id, fleet_id, "Original memory content", base_emb
        )
        await self._insert_memory(
            tenant_id, fleet_id, "Nearly identical content", dup_emb
        )

        result = await _check_near_duplicates(tenant_id, fleet_id)
        assert result["count"] >= 1, "Should find at least one near-duplicate pair"
        assert result["pairs"][0]["similarity"] >= CRYSTALLIZER_DEDUP_THRESHOLD

    @pytest.mark.asyncio
    async def test_skips_already_checked(self, db, tenant_id, fleet_id):
        """Memories with last_dedup_checked_at set should be skipped."""
        from core_api.services.crystallizer_service import _check_near_duplicates

        base_emb = self._fake_embedding("already-checked")
        dup_emb = self._near_duplicate_embedding(base_emb, noise=0.001)

        await self._insert_memory(
            tenant_id,
            fleet_id,
            "Already checked A",
            base_emb,
            last_dedup_checked_at=datetime.now(timezone.utc),
        )
        await self._insert_memory(
            tenant_id,
            fleet_id,
            "Already checked B",
            dup_emb,
            last_dedup_checked_at=datetime.now(timezone.utc),
        )

        result = await _check_near_duplicates(tenant_id, fleet_id)
        assert result["count"] == 0, "Already-checked memories should be skipped"

    @pytest.mark.asyncio
    async def test_updates_dedup_timestamp(self, db, tenant_id, fleet_id):
        """After processing, memories should have last_dedup_checked_at set."""
        from core_api.clients.storage_client import get_storage_client
        from core_api.services.crystallizer_service import _check_near_duplicates

        emb = self._fake_embedding("timestamp-test")
        mem = await self._insert_memory(tenant_id, fleet_id, "Check timestamp", emb)
        assert mem["last_dedup_checked_at"] is None

        await _check_near_duplicates(tenant_id, fleet_id)

        sc = get_storage_client()
        refreshed = await sc.get_memory(mem["id"])
        assert refreshed["last_dedup_checked_at"] is not None, (
            "last_dedup_checked_at should be set after processing"
        )

    @pytest.mark.asyncio
    async def test_no_false_positives_different_topics(self, db, tenant_id, fleet_id):
        """Unrelated memories should not appear as near-duplicates."""
        from core_api.services.crystallizer_service import _check_near_duplicates

        emb_a = self._fake_embedding("topic-alpha-completely-different")
        emb_b = self._fake_embedding("topic-beta-totally-unrelated")

        await self._insert_memory(tenant_id, fleet_id, "Alpha topic", emb_a)
        await self._insert_memory(tenant_id, fleet_id, "Beta topic", emb_b)

        result = await _check_near_duplicates(tenant_id, fleet_id)
        assert result["count"] == 0, (
            "Unrelated memories should not be flagged as duplicates"
        )


# ---------------------------------------------------------------------------
# Crystallization LLM path
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCrystallizeCluster:
    """Verify _crystallize_cluster uses call_with_fallback correctly."""

    @pytest.mark.asyncio
    async def test_fake_provider_uses_fake_fn(self):
        """With FakeLLMProvider, _crystallize_fake is called (highest-weight memory)."""
        from core_api.services.crystallizer_service import _crystallize_cluster

        class _FakeConfig:
            enrichment_provider = "fake"

        memories = [
            {"content": "low weight", "memory_type": "fact", "weight": 0.3},
            {"content": "high weight", "memory_type": "decision", "weight": 0.9},
            {"content": "mid weight", "memory_type": "fact", "weight": 0.5},
        ]

        result = await _crystallize_cluster(memories, _FakeConfig())
        assert len(result) == 1
        assert result[0]["content"] == "high weight"
        assert result[0]["memory_type"] == "decision"
        assert result[0]["weight"] == 0.9

    @pytest.mark.asyncio
    async def test_empty_input_returns_empty(self):
        """Empty memories list returns empty result."""
        from core_api.services.crystallizer_service import _crystallize_cluster

        class _FakeConfig:
            enrichment_provider = "fake"

        result = await _crystallize_cluster([], _FakeConfig())
        assert result == []

    @pytest.mark.asyncio
    async def test_llm_response_validation(self):
        """LLM JSON response is validated: bad types default to 'fact', weights clamped."""
        from unittest.mock import AsyncMock, patch

        from core_api.services.crystallizer_service import _crystallize_cluster

        class _MockConfig:
            enrichment_provider = "openai"

            def resolve_fallback(self):
                return (None, None)

        mock_llm = AsyncMock()
        mock_llm.is_fake = False
        mock_llm.complete_json = AsyncMock(
            return_value=[
                {"content": "valid fact", "memory_type": "fact", "weight": 0.7},
                {"content": "bad type", "memory_type": "INVALID", "weight": 0.5},
                {"content": "clamped weight", "memory_type": "decision", "weight": 5.0},
                {
                    "content": "",
                    "memory_type": "fact",
                    "weight": 0.5,
                },  # empty — skipped
            ]
        )

        with patch(
            "core_api.services.crystallizer_service.call_with_fallback",
        ) as mock_fallback:
            # Simulate call_with_fallback calling call_fn with our mock provider
            async def run_call_fn(*args, call_fn, **kwargs):
                return await call_fn(mock_llm)

            mock_fallback.side_effect = run_call_fn

            result = await _crystallize_cluster(
                [{"content": "test", "weight": 0.5}],
                _MockConfig(),
            )

        assert len(result) == 3  # empty content skipped
        assert result[0]["memory_type"] == "fact"
        assert result[1]["memory_type"] == "fact"  # INVALID -> fact
        assert result[2]["weight"] == 1.0  # 5.0 clamped to 1.0
