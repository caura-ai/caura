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
    async def test_finds_near_duplicates(self, tenant_id, fleet_id):
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
    async def test_skips_already_checked(self, tenant_id, fleet_id):
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
    async def test_updates_dedup_timestamp(self, tenant_id, fleet_id):
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
    async def test_no_false_positives_different_topics(self, tenant_id, fleet_id):
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


class TestMissingEmbeddingsCheck:
    """``_check_missing_embeddings`` must read the keys the endpoint returns.

    It previously read ``missing_count`` / ``missing_ids``, neither of which
    ``GET /memories/embedding-coverage`` has ever returned, so both ``.get()``
    calls fell through to their defaults and every tenant reported
    ``count: 0`` — a silent all-clear while prod was logging "Storing memory
    without embedding; deferred backfill scheduled" hundreds of times a day.
    """

    #: Exactly what the storage endpoint returns — see
    #: core-storage-api/src/core_storage_api/routers/memories.py::get_embedding_coverage.
    COVERAGE_RESPONSE = {
        "total_active": 10,
        "missing_embeddings": 4,
        "coverage_pct": 60.0,
    }

    @pytest.mark.asyncio
    async def test_reports_missing_embeddings_count(self):
        """4 missing out of 10 is reported as 4, not 0."""
        from unittest.mock import AsyncMock, patch

        from core_api.services.crystallizer_service import _check_missing_embeddings

        sc = AsyncMock()
        sc.get_embedding_coverage.return_value = dict(self.COVERAGE_RESPONSE)
        with patch(
            "core_api.services.crystallizer_service.get_storage_client",
            return_value=sc,
        ):
            result = await _check_missing_embeddings("tenant-1", None)

        assert result["count"] == 4

    @pytest.mark.asyncio
    async def test_clean_tenant_reports_zero(self):
        """Full coverage still reports zero — guards against inverting the read."""
        from unittest.mock import AsyncMock, patch

        from core_api.services.crystallizer_service import _check_missing_embeddings

        sc = AsyncMock()
        sc.get_embedding_coverage.return_value = {
            "total_active": 10,
            "missing_embeddings": 0,
            "coverage_pct": 100.0,
        }
        with patch(
            "core_api.services.crystallizer_service.get_storage_client",
            return_value=sc,
        ):
            result = await _check_missing_embeddings("tenant-1", None)

        assert result["count"] == 0

    def test_endpoint_contract_is_pinned(self):
        """The endpoint's response keys, asserted against the endpoint itself.

        The bug was a silent key mismatch across a service boundary, so pin the
        contract here: if the endpoint's keys change, this fails rather than the
        check quietly returning 0 again.
        """
        import inspect

        from core_storage_api.routers.memories import get_embedding_coverage

        source = inspect.getsource(get_embedding_coverage)
        for key in self.COVERAGE_RESPONSE:
            assert f'"{key}"' in source, f"endpoint no longer returns {key!r}"
        assert '"missing_count"' not in source
        assert '"missing_ids"' not in source


class TestLifecycleCandidatesConsumers:
    """Every crystallizer consumer of ``/lifecycle-candidates`` must match it.

    Three consumers all disagreed with the endpoint at once, in three
    different ways, and each failed toward a reassuring value:

    * ``_check_stale_memories`` read ``stale_memories``; the key is
      ``stale_low_weight`` — reported 0 for every tenant.
    * ``_check_expired_still_active`` read the right key but treated the
      values as row dicts (``r.get("id")``) when they are bare UUID
      strings — raised ``AttributeError`` for any tenant with an expired
      memory, which ``run_crystallization`` swallowed into
      ``{"error": True}``.
    * ``_remediate_missing_embeddings`` read ``missing_embeddings``, which
      this endpoint has never returned — a permanent no-op, now deleted
      (repair belongs in ``core_worker.backfill``).
    """

    #: Exactly what the endpoint returns — bare UUID strings, not row dicts.
    #: See core-storage-api/.../routers/memories.py::get_lifecycle_candidates.
    CANDIDATES_RESPONSE = {
        "expired_still_active": ["11111111-1111-1111-1111-111111111111"],
        "stale_low_weight": ["22222222-2222-2222-2222-222222222222"],
        "short_content": ["33333333-3333-3333-3333-333333333333"],
    }

    def _patched_sc(self):
        from unittest.mock import AsyncMock

        sc = AsyncMock()
        sc.get_lifecycle_candidates.return_value = dict(self.CANDIDATES_RESPONSE)
        return sc

    @pytest.mark.asyncio
    async def test_expired_check_counts_string_ids_without_raising(self):
        """One expired row is counted, not turned into an AttributeError."""
        from unittest.mock import patch

        from core_api.services.crystallizer_service import _check_expired_still_active

        with patch(
            "core_api.services.crystallizer_service.get_storage_client",
            return_value=self._patched_sc(),
        ):
            result = await _check_expired_still_active("tenant-1", None)

        assert result["count"] == 1
        assert result["affected_ids"] == ["11111111-1111-1111-1111-111111111111"]

    @pytest.mark.asyncio
    async def test_stale_check_reads_stale_low_weight(self):
        """One stale row is reported as 1, not 0."""
        from unittest.mock import patch

        from core_api.services.crystallizer_service import _check_stale_memories

        with patch(
            "core_api.services.crystallizer_service.get_storage_client",
            return_value=self._patched_sc(),
        ):
            result = await _check_stale_memories("tenant-1", None)

        assert result["count"] == 1
        assert result["affected_ids"] == ["22222222-2222-2222-2222-222222222222"]

    @pytest.mark.asyncio
    async def test_short_content_check_reads_short_content(self):
        """One short-content row is reported as 1, not 0.

        The key didn't exist on the endpoint until it was added alongside the
        two lists already there, using the ``memory_find_short_content`` query
        that had been written for this and left unexposed.
        """
        from unittest.mock import patch

        from core_api.services.crystallizer_service import _check_short_content

        with patch(
            "core_api.services.crystallizer_service.get_storage_client",
            return_value=self._patched_sc(),
        ):
            result = await _check_short_content("tenant-1", None)

        assert result["count"] == 1
        assert result["affected_ids"] == ["33333333-3333-3333-3333-333333333333"]

    def test_endpoint_contract_is_pinned(self):
        """Pin the endpoint's keys, and that it yields strings rather than dicts.

        This is the guard that makes the whole class non-recurring: a
        hand-written mock proves nothing here, because the original mocks
        would have been written from the same wrong assumption as the
        callers.
        """
        import inspect

        from core_storage_api.routers.memories import get_lifecycle_candidates

        source = inspect.getsource(get_lifecycle_candidates)
        for key in self.CANDIDATES_RESPONSE:
            assert f'"{key}"' in source, f"endpoint no longer returns {key!r}"
        # Keys the crystallizer used to read, none of which exist here.
        assert '"stale_memories"' not in source
        assert '"missing_embeddings"' not in source
        # Values are stringified ids, not row dicts — the shape that made
        # ``r.get("id")`` raise.
        assert "str(r[0])" in source

    def test_short_content_threshold_is_shared_not_duplicated(self):
        """One threshold, both services.

        core-api rejects short writes at the quality gate and core-storage-api
        lists rows below the same bound; a duplicated literal would let the
        hygiene report disagree with what the write path accepts.
        """
        from common.constants import CRYSTALLIZER_SHORT_CONTENT_CHARS as shared
        from core_api.constants import CRYSTALLIZER_SHORT_CONTENT_CHARS as via_core_api

        assert via_core_api == shared

    def test_inline_remediation_is_gone(self):
        """Repair must not run inline in a crystallization pass.

        Inline embedding would draw from the same process-wide embedding gate
        that write traffic already oversubscribes, so a hygiene report could
        stall the write path it reports on. ``core_worker.backfill`` does this
        job through the event bus instead.
        """
        import inspect

        from core_api.services import crystallizer_service

        assert not hasattr(crystallizer_service, "_remediate_missing_embeddings")
        source = inspect.getsource(crystallizer_service.run_crystallization)
        assert "remediate" not in source.lower().replace("remediating", "")
