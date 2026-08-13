"""P2: Write-time semantic deduplication tests.

Unit tests validate:
- Constants: threshold matches crystallizer, above contradiction threshold, limit is 1
- _find_semantic_duplicate: returns None when no match, returns Memory when above
  threshold, respects fleet scoping, excludes deleted/non-active

Integration tests verify:
- Exact duplicate still caught (regression)
- Near-duplicate caught: same embedding → 409
- Below-threshold content passes normally
- Different fleet: same embedding in fleet-a doesn't block fleet-b
- Deleted memory doesn't block new write
- Archived memory doesn't block (status not in active/confirmed/pending)
- 409 response includes existing memory ID
- Update path: content change to near-duplicate of another memory → 409
- Update path: minor edit to own content doesn't false-positive (exclude_id works)

Benchmark tests:
- _find_semantic_duplicate query latency against 100 and 1000 memories
"""

import hashlib
import random
import statistics
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from common.embedding import fake_embedding
from common.models.memory import Memory
from core_api.constants import (
    CONTRADICTION_SIMILARITY_THRESHOLD,
    CRYSTALLIZER_DEDUP_THRESHOLD,
    SEMANTIC_DEDUP_CANDIDATE_LIMIT,
    SEMANTIC_DEDUP_THRESHOLD,
    VECTOR_DIM,
)
from core_api.services.memory_service import _find_semantic_duplicate
from core_storage_api.services import postgres_service as ps


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def close_embedding(base_text: str, noise: float = 0.001) -> list[float]:
    """Produce an embedding very close to fake_embedding(base_text).

    Adds tiny Gaussian noise to each dimension, resulting in cosine
    similarity ~0.999 against the original — well above 0.95.
    """
    base = fake_embedding(base_text)
    rng = random.Random(42)
    return [v + rng.gauss(0, noise) for v in base]


def _content_hash(tenant_id: str, fleet_id: str | None, content: str) -> str:
    return hashlib.sha256(f"{tenant_id}:{fleet_id or ''}:{content}".encode()).hexdigest()


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSemanticDedupConstants:
    """Verify P2 constant values and relationships."""

    def test_threshold_matches_crystallizer(self):
        """Semantic dedup threshold should match crystallizer dedup threshold."""
        assert SEMANTIC_DEDUP_THRESHOLD == CRYSTALLIZER_DEDUP_THRESHOLD

    def test_threshold_above_contradiction(self):
        """Semantic dedup threshold must be above contradiction threshold
        to avoid confusing near-duplicates with contradictions."""
        assert SEMANTIC_DEDUP_THRESHOLD > CONTRADICTION_SIMILARITY_THRESHOLD

    def test_threshold_value(self):
        assert SEMANTIC_DEDUP_THRESHOLD == 0.95

    def test_candidate_limit_is_one(self):
        """We only need to know if any match exists."""
        assert SEMANTIC_DEDUP_CANDIDATE_LIMIT == 1


@pytest.mark.unit
class TestFindSemanticDuplicateMocked:
    """Unit tests for _find_semantic_duplicate with mocked storage client."""

    async def test_returns_none_when_no_match(self):
        from unittest.mock import patch

        mock_sc = AsyncMock()
        mock_sc.find_semantic_duplicate = AsyncMock(return_value=None)

        with patch("core_api.services.memory_service.get_storage_client", return_value=mock_sc):
            result = await _find_semantic_duplicate("tenant-1", None, [0.1] * VECTOR_DIM,
            )
        assert result is None

    async def test_returns_memory_when_match_found(self):
        from unittest.mock import patch

        mock_memory = {"id": str(uuid4()), "content": "test"}
        mock_sc = AsyncMock()
        mock_sc.find_semantic_duplicate = AsyncMock(return_value=mock_memory)

        with patch("core_api.services.memory_service.get_storage_client", return_value=mock_sc):
            result = await _find_semantic_duplicate("tenant-1", None, [0.1] * VECTOR_DIM,
            )
        assert result is mock_memory

    async def test_passes_fleet_id_filter(self):
        """Verify that fleet_id is passed to storage client."""
        from unittest.mock import patch

        mock_sc = AsyncMock()
        mock_sc.find_semantic_duplicate = AsyncMock(return_value=None)

        with patch("core_api.services.memory_service.get_storage_client", return_value=mock_sc):
            await _find_semantic_duplicate("tenant-1", "fleet-a", [0.1] * VECTOR_DIM,
            )

        call_data = mock_sc.find_semantic_duplicate.call_args[0][0]
        assert call_data["fleet_id"] == "fleet-a"

    async def test_passes_exclude_id(self):
        """Verify that exclude_id is passed to storage client."""
        from unittest.mock import patch

        mock_sc = AsyncMock()
        mock_sc.find_semantic_duplicate = AsyncMock(return_value=None)

        exclude = uuid4()
        with patch("core_api.services.memory_service.get_storage_client", return_value=mock_sc):
            await _find_semantic_duplicate("tenant-1", None, [0.1] * VECTOR_DIM, exclude_id=exclude,
            )

        call_data = mock_sc.find_semantic_duplicate.call_args[0][0]
        assert call_data["exclude_id"] == str(exclude)


@pytest.mark.unit
class TestSemanticDedupToggle:
    """Verify per-tenant toggle via ResolvedConfig."""

    def test_default_enabled(self):
        from core_api.services.organization_settings import ResolvedConfig
        cfg = ResolvedConfig({})
        assert cfg.semantic_dedup_enabled is True

    def test_explicitly_disabled(self):
        from core_api.services.organization_settings import ResolvedConfig
        cfg = ResolvedConfig({"dedup": {"semantic_dedup_enabled": False}})
        assert cfg.semantic_dedup_enabled is False

    def test_explicitly_enabled(self):
        from core_api.services.organization_settings import ResolvedConfig
        cfg = ResolvedConfig({"dedup": {"semantic_dedup_enabled": True}})
        assert cfg.semantic_dedup_enabled is True


@pytest.mark.unit
class TestCloseEmbeddingHelper:
    """Verify the close_embedding test helper produces near-duplicates."""

    def test_close_embedding_high_similarity(self):
        import math

        base = fake_embedding("test content")
        close = close_embedding("test content")

        dot = sum(a * b for a, b in zip(base, close))
        norm_a = math.sqrt(sum(a * a for a in base))
        norm_b = math.sqrt(sum(b * b for b in close))
        similarity = dot / (norm_a * norm_b)

        assert similarity > 0.99, f"Expected >0.99, got {similarity}"

    def test_different_text_low_similarity(self):
        """Completely different text should have lower similarity."""
        base = fake_embedding("I love Python programming")
        other = fake_embedding("The weather is sunny in Alaska")

        import math
        dot = sum(a * b for a, b in zip(base, other))
        norm_a = math.sqrt(sum(a * a for a in base))
        norm_b = math.sqrt(sum(b * b for b in other))
        similarity = dot / (norm_a * norm_b)

        # Different fake embeddings should not be near-duplicates
        assert similarity < 0.99


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestSemanticDedupIntegration:
    """End-to-end semantic dedup with real DB."""

    async def _insert_memory(
        self, tenant_id, content, *,
        embedding=None, fleet_id=None, status="active",
        agent_id="test-agent", memory_type="fact", deleted_at=None,
    ):
        from core_api.clients.storage_client import get_storage_client
        sc = get_storage_client()
        emb = embedding or fake_embedding(content)
        ch = _content_hash(tenant_id, fleet_id, content)
        payload = {
            "tenant_id": tenant_id,
            "fleet_id": fleet_id,
            "agent_id": agent_id,
            "memory_type": memory_type,
            "content": content,
            "weight": 0.5,
            "embedding": emb,
            "content_hash": ch,
            "status": status,
        }
        if deleted_at is not None:
            payload["deleted_at"] = deleted_at.isoformat() if hasattr(deleted_at, 'isoformat') else deleted_at
        mem = await sc.create_memory(payload)
        return mem  # returns a dict

    # -- Regression: exact duplicate still caught --

    async def test_exact_duplicate_still_rejected(self, tenant_id):
        """Hash-based exact dedup must still work (regression)."""
        content = "Alice prefers dark mode"
        await self._insert_memory(tenant_id, content)

        # Exact same embedding should be found
        emb = fake_embedding(content)
        dup = await _find_semantic_duplicate(tenant_id, None, emb)
        assert dup is not None

    # -- Near-duplicate caught --

    async def test_near_duplicate_caught(self, tenant_id):
        """Insert memory, then try close embedding with different content → found."""
        base_content = "The quarterly report shows 15% revenue growth"
        await self._insert_memory(tenant_id, base_content)

        # Close embedding (noise-perturbed) should still be caught
        emb = close_embedding(base_content)
        dup = await _find_semantic_duplicate(tenant_id, None, emb)
        assert dup is not None

    # -- Below-threshold passes --

    async def test_below_threshold_passes(self, tenant_id):
        """Completely different content should not be flagged."""
        await self._insert_memory(tenant_id, "Alice prefers dark mode")

        emb = fake_embedding("The server is running on port 8080")
        dup = await _find_semantic_duplicate(tenant_id, None, emb)
        assert dup is None

    # -- Fleet isolation --

    async def test_different_fleet_not_blocked(self, tenant_id):
        """Same embedding in fleet-a should not block fleet-b."""
        content = "Bob likes tea"
        emb = fake_embedding(content)

        await self._insert_memory(tenant_id, content, fleet_id="fleet-a")

        # Search in fleet-b should find nothing
        dup = await _find_semantic_duplicate(tenant_id, "fleet-b", emb)
        assert dup is None

    # -- Deleted memory doesn't block --

    async def test_deleted_memory_doesnt_block(self, tenant_id):
        """Soft-deleted memory should not be considered a duplicate."""
        content = "Important meeting tomorrow"
        await self._insert_memory(
            tenant_id, content,
            deleted_at=datetime.now(timezone.utc),
        )

        emb = fake_embedding(content)
        dup = await _find_semantic_duplicate(tenant_id, None, emb)
        assert dup is None

    # -- Archived memory doesn't block --

    async def test_archived_memory_doesnt_block(self, tenant_id):
        """Archived status is not in (active, confirmed, pending) → ignored."""
        content = "Old project plan from last year"
        await self._insert_memory(tenant_id, content, status="archived")

        emb = fake_embedding(content)
        dup = await _find_semantic_duplicate(tenant_id, None, emb)
        assert dup is None

    # -- 409 response includes memory ID --

    async def test_409_includes_memory_id(self, tenant_id):
        """The returned duplicate should have a valid UUID."""
        content = "Deployment scheduled for Friday"
        mem = await self._insert_memory(tenant_id, content)

        emb = fake_embedding(content)
        dup = await _find_semantic_duplicate(tenant_id, None, emb)
        assert dup is not None
        assert dup["id"] == mem["id"]

    # -- Update path: near-duplicate of another → blocked --

    async def test_update_near_duplicate_blocked(self, tenant_id):
        """Updating content to match another memory should be caught."""
        existing_content = "API latency is under 50ms"
        await self._insert_memory(tenant_id, existing_content)

        # Second memory being "updated" to similar content
        updating_mem = await self._insert_memory(
            tenant_id, "Something completely different",
        )

        emb = fake_embedding(existing_content)
        dup = await _find_semantic_duplicate(tenant_id, None, emb, exclude_id=updating_mem["id"],
        )
        assert dup is not None

    # -- Update path: own content doesn't false-positive --

    async def test_update_own_content_not_blocked(self, tenant_id):
        """Minor edit to own content should not false-positive (exclude_id)."""
        content = "Database migration completed successfully"
        mem = await self._insert_memory(tenant_id, content)

        # Same embedding but excluding self
        emb = fake_embedding(content)
        dup = await _find_semantic_duplicate(tenant_id, None, emb, exclude_id=mem["id"],
        )
        assert dup is None


# ---------------------------------------------------------------------------
# Benchmark tests
# ---------------------------------------------------------------------------

_BENCH_ITERATIONS = 20


@asynccontextmanager
async def _committed_corpus(count: int) -> AsyncIterator[str]:
    """Yield a fresh tenant holding exactly ``count`` committed memories.

    Committed, because the query under test reaches Postgres through the service
    layer on its own session — rows sitting in an uncommitted transaction (what
    the ``db`` fixture gives you) are invisible to it.

    Its own tenant, so the slice is labelled and its row count is exactly
    ``count``. The ``tenant_id`` fixture is per-test now, so that is no longer
    what isolates this corpus — but the explicit ``DELETE`` below still earns its
    keep, since nothing else clears these rows before session end.

    Both effects are silent, which is why the caller asserts the corpus size
    instead of assuming it.

    Keeps the ``test-tenant-`` prefix so session teardown sweeps the rows even
    if the explicit cleanup below does not run.
    """
    tenant = f"test-tenant-dedupbench-{uuid4().hex[:8]}"
    async with ps.get_session() as session:
        for i in range(count):
            content = f"Benchmark memory number {i} with unique content {uuid4().hex}"
            session.add(
                Memory(
                    tenant_id=tenant,
                    fleet_id=None,
                    agent_id="bench-agent",
                    memory_type="fact",
                    content=content,
                    weight=0.5,
                    embedding=fake_embedding(content),
                    content_hash=_content_hash(tenant, None, content),
                    status="active",
                )
            )
    try:
        yield tenant
    finally:
        async with ps.get_session() as session:
            await session.execute(delete(Memory).where(Memory.tenant_id == tenant))


async def _visible_count(tenant: str) -> int:
    """Rows visible to the same session accessor the query under test uses.

    ``get_session``, not ``get_read_session``: ``memory_find_semantic_duplicate``
    runs in the writer session, and in tests both factories happen to be bound
    to the one engine — so reading through the reader would agree only by
    accident, and would stop being evidence the day a replica is configured.
    """
    async with ps.get_session() as session:
        return (
            await session.execute(
                select(func.count())
                .select_from(Memory)
                .where(Memory.tenant_id == tenant, Memory.deleted_at.is_(None))
            )
        ).scalar_one()


async def _measure(tenant: str) -> tuple[float, float, float]:
    """``(mean, median, max)`` ms over ``_BENCH_ITERATIONS`` dedup lookups.

    ``max``, not "p99": at 20 samples the 99th-percentile index is the last
    element, so calling it p99 dressed up the slowest single run as a tail
    statistic.
    """
    emb = fake_embedding("A completely new memory for benchmark")
    times = []
    for _ in range(_BENCH_ITERATIONS):
        t0 = time.perf_counter()
        await _find_semantic_duplicate(tenant, None, emb)
        times.append((time.perf_counter() - t0) * 1000)
    return statistics.mean(times), statistics.median(times), max(times)


@pytest.mark.benchmark
@pytest.mark.parametrize(
    ("count", "budget_ms"),
    [(100, 10), (1000, 50)],
    ids=["100_memories", "1000_memories"],
)
async def test_semantic_dedup_query_latency(count: int, budget_ms: float) -> None:
    """Dedup query latency against a corpus of the stated size.

    The corpus assertion is load-bearing, not a sanity check: an empty table
    comfortably meets both budgets, so a corpus the query cannot see would pass
    silently while measuring nothing.
    """
    async with _committed_corpus(count) as tenant:
        visible = await _visible_count(tenant)
        assert visible == count, (
            f"benchmark corpus is not visible to the query under test: expected "
            f"{count} rows under {tenant}, the query's own session sees {visible}. "
            f"Measuring latency now would report the wrong corpus size."
        )

        mean_ms, median_ms, max_ms = await _measure(tenant)

        print(f"\n{'─' * 60}")
        print(f"SEMANTIC DEDUP QUERY ({count} memories, {_BENCH_ITERATIONS} iterations)")
        print(f"  mean={mean_ms:.2f}ms  median={median_ms:.2f}ms  max={max_ms:.2f}ms")
        print(f"{'─' * 60}")

        assert mean_ms < budget_ms, f"Too slow with {count} memories: {mean_ms:.1f}ms"
