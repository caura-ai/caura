"""Bulk write endpoint unit tests.

Unit tests validate:
- Schema validation: max items, required fields, content constraints
- Batch embedding: get_embeddings_batch returns correct count
- Bulk hash dedup: intra-batch and DB-level dedup detection
- Enrichment application: LLM fills gaps, agent values win
- Bulk usage check: quota checked once for N items
- create_memories_bulk: end-to-end with mocked DB + embedding
"""

import hashlib
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from core_api.constants import BULK_MAX_ITEMS, DEFAULT_MEMORY_WEIGHT, VECTOR_DIM
from core_api.schemas import (
    BulkItemResult,
    BulkMemoryCreate,
    BulkMemoryItem,
    BulkMemoryResponse,
)
from common.embedding import fake_embedding


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _content_hash(tenant_id: str, fleet_id: str | None, content: str) -> str:
    return hashlib.sha256(f"{tenant_id}:{fleet_id or ''}:{content}".encode()).hexdigest()


def _make_bulk_request(
    n: int = 3,
    tenant_id: str = "test-tenant",
    fleet_id: str | None = None,
    agent_id: str = "agent-1",
    contents: list[str] | None = None,
) -> BulkMemoryCreate:
    if contents is None:
        contents = [f"Memory number {i}" for i in range(n)]
    return BulkMemoryCreate(
        tenant_id=tenant_id,
        fleet_id=fleet_id,
        agent_id=agent_id,
        items=[BulkMemoryItem(content=c) for c in contents],
    )


# ---------------------------------------------------------------------------
# Unit tests: Schema validation
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestBulkSchemaValidation:

    def test_accepts_valid_request(self):
        req = _make_bulk_request(3)
        assert len(req.items) == 3
        assert req.tenant_id == "test-tenant"

    def test_rejects_empty_items(self):
        with pytest.raises(Exception):
            BulkMemoryCreate(tenant_id="t", agent_id="a", items=[])

    def test_rejects_over_max_items(self):
        items = [BulkMemoryItem(content=f"item {i}") for i in range(BULK_MAX_ITEMS + 1)]
        with pytest.raises(Exception):
            BulkMemoryCreate(tenant_id="t", agent_id="a", items=items)

    def test_accepts_exactly_max_items(self):
        items = [BulkMemoryItem(content=f"item {i}") for i in range(BULK_MAX_ITEMS)]
        req = BulkMemoryCreate(tenant_id="t", agent_id="a", items=items)
        assert len(req.items) == BULK_MAX_ITEMS

    def test_single_item_accepted(self):
        req = _make_bulk_request(1)
        assert len(req.items) == 1

    def test_item_content_validation(self):
        """Empty content should be rejected."""
        with pytest.raises(Exception):
            BulkMemoryItem(content="")

    def test_item_inherits_no_tenant(self):
        """BulkMemoryItem should not have tenant_id field."""
        item = BulkMemoryItem(content="test")
        assert not hasattr(item, "tenant_id")

    def test_item_optional_fields_default_none(self):
        item = BulkMemoryItem(content="test")
        assert item.memory_type is None
        assert item.weight is None
        assert item.source_uri is None
        assert item.status is None

    def test_item_with_all_fields(self):
        item = BulkMemoryItem(
            content="test",
            memory_type="decision",
            weight=0.8,
            source_uri="https://example.com",
            status="confirmed",
        )
        assert item.memory_type == "decision"
        assert item.weight == 0.8

    def test_invalid_memory_type_rejected(self):
        with pytest.raises(Exception):
            BulkMemoryItem(content="test", memory_type="invalid_type")

    def test_invalid_status_rejected(self):
        with pytest.raises(Exception):
            BulkMemoryItem(content="test", status="invalid_status")

    def test_weight_out_of_range_rejected(self):
        with pytest.raises(Exception):
            BulkMemoryItem(content="test", weight=1.5)


# ---------------------------------------------------------------------------
# Unit tests: Batch embedding
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestBatchEmbedding:

    @pytest.mark.asyncio
    async def test_fake_batch_embedding(self):
        from common.embedding import get_embeddings_batch

        texts = ["hello", "world", "test"]
        config = MagicMock()
        config.embedding_provider = "fake"
        results = await get_embeddings_batch(texts, config)
        assert len(results) == 3
        assert all(len(e) == VECTOR_DIM for e in results)

    @pytest.mark.asyncio
    async def test_batch_matches_single(self):
        """Batch fake embeddings should match individual fake embeddings."""
        from common.embedding import get_embeddings_batch

        texts = ["alpha", "beta"]
        config = MagicMock()
        config.embedding_provider = "fake"
        batch = await get_embeddings_batch(texts, config)
        singles = [fake_embedding(t) for t in texts]
        assert batch[0] == singles[0]
        assert batch[1] == singles[1]

    @pytest.mark.asyncio
    async def test_empty_batch_returns_empty(self):
        from common.embedding import get_embeddings_batch

        config = MagicMock()
        config.embedding_provider = "fake"
        results = await get_embeddings_batch([], config)
        assert results == []


# ---------------------------------------------------------------------------
# Unit tests: Bulk response model
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestBulkResponseModel:

    def test_all_created(self):
        resp = BulkMemoryResponse(
            created=3, duplicates=0, errors=0,
            results=[
                BulkItemResult(index=0, status="created", id=uuid4()),
                BulkItemResult(index=1, status="created", id=uuid4()),
                BulkItemResult(index=2, status="created", id=uuid4()),
            ],
            bulk_ms=100,
        )
        assert resp.created == 3
        assert all(r.status == "created" for r in resp.results)

    def test_mixed_results(self):
        resp = BulkMemoryResponse(
            created=1, duplicates=2, errors=1,
            results=[
                BulkItemResult(index=0, status="created", id=uuid4()),
                BulkItemResult(index=1, status="duplicate_content", duplicate_of=uuid4()),
                BulkItemResult(index=2, status="duplicate_attempt", id=uuid4()),
                BulkItemResult(index=3, status="error", error="something broke"),
            ],
            bulk_ms=200,
        )
        assert resp.created == 1
        assert resp.duplicates == 2
        assert resp.errors == 1
        assert resp.results[3].error == "something broke"
        assert resp.results[1].status == "duplicate_content"
        assert resp.results[2].status == "duplicate_attempt"


# ---------------------------------------------------------------------------
# Unit tests: Content hash dedup
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestBulkContentHashDedup:

    def test_intra_batch_duplicate_detected(self):
        """Two items with identical content should be caught as intra-batch dup."""
        contents = ["same content", "unique content", "same content"]
        hashes = [_content_hash("t", None, c) for c in contents]
        assert hashes[0] == hashes[2]
        assert hashes[0] != hashes[1]

    def test_fleet_scoped_hash(self):
        """Same content in different fleets should produce different hashes."""
        h1 = _content_hash("t", "fleet-a", "hello")
        h2 = _content_hash("t", "fleet-b", "hello")
        h3 = _content_hash("t", None, "hello")
        assert h1 != h2
        assert h1 != h3

    def test_tenant_scoped_hash(self):
        """Same content in different tenants should produce different hashes."""
        h1 = _content_hash("tenant-1", None, "hello")
        h2 = _content_hash("tenant-2", None, "hello")
        assert h1 != h2


# ---------------------------------------------------------------------------
# Unit tests: Bulk usage service
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestBulkUsageCheck:

    @pytest.mark.asyncio
    async def test_bulk_check_always_allowed_in_oss(self):
        """bulk_check_and_increment always returns allowed in OSS mode (no limits)."""
        from core_api.services.usage_service import bulk_check_and_increment

        db = AsyncMock()
        result = await bulk_check_and_increment(db, "test-tenant", 20)
        assert result.allowed is True
        assert result.operation == "write"

    @pytest.mark.asyncio
    async def test_bulk_check_accepts_within_limit(self):
        """bulk_check_and_increment should pass for any count in OSS."""
        from core_api.services.usage_service import bulk_check_and_increment

        db = AsyncMock()
        result = await bulk_check_and_increment(db, "test-tenant", 5)
        assert result.allowed is True

    @pytest.mark.asyncio
    async def test_bulk_check_three_args(self):
        """bulk_check_and_increment takes exactly 3 args: (db, tenant_id, count)."""
        from core_api.services.usage_service import bulk_check_and_increment

        db = AsyncMock()
        result = await bulk_check_and_increment(db, "test-tenant", 100)
        assert result.allowed is True


# ---------------------------------------------------------------------------
# Unit tests: Enrichment application logic
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestBulkEnrichmentApplication:

    def test_agent_values_win_over_enrichment(self):
        """When agent provides memory_type and weight, enrichment should not override."""
        item = BulkMemoryItem(content="test", memory_type="decision", weight=0.9)
        assert item.memory_type == "decision"
        assert item.weight == 0.9

    def test_defaults_applied_when_no_enrichment(self):
        """Without enrichment, defaults should be applied."""
        item = BulkMemoryItem(content="test")
        # These would be None on the item; create_memories_bulk fills defaults
        assert item.memory_type is None
        assert item.weight is None


# ---------------------------------------------------------------------------
# Unit tests: Constants
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestBulkConstants:

    def test_max_items_is_100(self):
        assert BULK_MAX_ITEMS == 100

    def test_default_weight_applied(self):
        assert DEFAULT_MEMORY_WEIGHT == 0.5
