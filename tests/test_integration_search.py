"""Integration tests: full search_memories pipeline with all P0 fixes.

Requires a running PostgreSQL instance with pgvector.
Set TEST_DATABASE_URL env var or use defaults (memclaw_test on localhost).

These tests exercise the actual SQL expressions — freshness, recall boost,
scoring blend, entity matching — end-to-end via the service layer.
"""

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text, update

from core_api.clients.storage_client import get_storage_client
from core_api.constants import (
    FRESHNESS_DECAY_DAYS,
)
from common.models.memory import Memory
from common.embedding import fake_embedding


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash(tenant_id, fleet_id, content):
    return hashlib.sha256(f"{tenant_id}:{fleet_id}:{content}".encode()).hexdigest()


async def _insert_memory(
    db,
    tenant_id,
    content,
    *,
    weight=0.5,
    fleet_id=None,
    agent_id="test-agent",
    memory_type="fact",
    status="active",
    created_at=None,
    ts_valid_start=None,
    ts_valid_end=None,
    recall_count=0,
    last_recalled_at=None,
):
    """Insert a memory via storage client for test setup."""
    emb = fake_embedding(content)
    sc = get_storage_client()

    payload: dict = {
        "tenant_id": tenant_id,
        "fleet_id": fleet_id,
        "agent_id": agent_id,
        "memory_type": memory_type,
        "content": content,
        "weight": weight,
        "embedding": emb,
        "content_hash": _hash(tenant_id, fleet_id, content),
        "status": status,
        "recall_count": recall_count,
        "visibility": "scope_team",
    }
    if last_recalled_at is not None:
        payload["last_recalled_at"] = last_recalled_at.isoformat()
    if ts_valid_start is not None:
        payload["ts_valid_start"] = ts_valid_start.isoformat()
    if ts_valid_end is not None:
        payload["ts_valid_end"] = ts_valid_end.isoformat()

    mem = await sc.create_memory(payload)

    # Populate search_vector for FTS scoring (normally done by app/trigger).
    # The storage client already committed the row, so we can update it via
    # the db session and commit so the storage API sessions can see it.
    await db.execute(
        text(
            "UPDATE memories SET search_vector = to_tsvector('english', :content) WHERE id = :id"
        ),
        {"content": content, "id": mem["id"]},
    )
    await db.commit()

    # Override created_at if provided (must do after create for server_default)
    if created_at:
        await db.execute(
            update(Memory).where(Memory.id == mem["id"]).values(created_at=created_at)
        )
        await db.commit()

    return mem


async def _insert_entity(db, tenant_id, name, entity_type="concept", fleet_id=None):
    """Insert an entity via storage client with search_vector populated."""
    sc = get_storage_client()
    entity = await sc.create_entity({
        "tenant_id": tenant_id,
        "fleet_id": fleet_id,
        "entity_type": entity_type,
        "canonical_name": name,
    })
    # Populate search_vector for FTS scoring
    await db.execute(
        text(
            "UPDATE entities SET search_vector = to_tsvector('english', :name) WHERE id = :id"
        ),
        {"name": name, "id": entity["id"]},
    )
    await db.commit()
    return entity


async def _link_memory_entity(db, memory_id, entity_id, role="mentioned"):
    sc = get_storage_client()
    await sc.create_entity_link({
        "memory_id": str(memory_id),
        "entity_id": str(entity_id),
        "role": role,
    })


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestSearchPipelineEndToEnd:
    """Full search_memories pipeline with all P0 fixes applied."""

    async def test_basic_search_returns_results(self, db, tenant_id):
        """Baseline: search returns stored memories sorted by relevance."""
        await _insert_memory(
            db, tenant_id, "Python is a programming language", weight=0.7
        )
        await _insert_memory(db, tenant_id, "The weather is sunny today", weight=0.7)

        from core_api.services.memory_service import search_memories

        results = await search_memories(db, tenant_id, "Python programming")
        assert len(results) >= 1
        # The Python memory should rank higher
        assert "Python" in results[0].content

    async def test_freshness_prefers_recent_events(self, db, tenant_id):
        """P0-2: memory about recent event ranks higher than old memory about same topic."""
        now = datetime.now(timezone.utc)

        # Old memory, no temporal fields — will decay normally
        await _insert_memory(
            db,
            tenant_id,
            "deployment system update completed successfully last quarter",
            weight=0.7,
            created_at=now - timedelta(days=FRESHNESS_DECAY_DAYS + 10),
        )
        # New memory with recent ts_valid_start
        await _insert_memory(
            db,
            tenant_id,
            "deployment system update critical patch applied today",
            weight=0.7,
            created_at=now - timedelta(days=80),
            ts_valid_start=now - timedelta(days=2),
        )

        from core_api.services.memory_service import search_memories

        results = await search_memories(db, tenant_id, "deployment system update")
        assert len(results) >= 2
        # The one with recent ts_valid_start should rank higher
        assert "critical patch" in results[0].content

    async def test_expired_memory_ranked_lower(self, db, tenant_id):
        """P0-2: expired memory (ts_valid_end in past) gets freshness floor."""
        now = datetime.now(timezone.utc)

        await _insert_memory(
            db,
            tenant_id,
            "Sprint deadline is next Friday for the analytics dashboard",
            weight=0.7,
            ts_valid_end=now - timedelta(days=1),  # expired yesterday
        )
        await _insert_memory(
            db,
            tenant_id,
            "Sprint deadline is this Friday for the analytics dashboard",
            weight=0.7,
            ts_valid_end=now + timedelta(days=5),  # still valid
        )

        from core_api.services.memory_service import search_memories

        results = await search_memories(db, tenant_id, "sprint deadline analytics")
        assert len(results) >= 2
        # Valid memory should rank above expired one
        assert "this Friday" in results[0].content

    async def test_recall_boost_decays_over_time(self, db, tenant_id):
        """P0-3: frequently recalled but stale memory doesn't dominate."""
        now = datetime.now(timezone.utc)

        # Memory A: recalled 50 times but 45 days ago (stale)
        await _insert_memory(
            db,
            tenant_id,
            "Architecture decision for microservices migration",
            weight=0.5,
            recall_count=50,
            last_recalled_at=now - timedelta(days=45),
        )
        # Memory B: recalled 2 times but just now (fresh)
        await _insert_memory(
            db,
            tenant_id,
            "Architecture decision for database sharding approach",
            weight=0.5,
            recall_count=2,
            last_recalled_at=now,
        )

        from core_api.services.memory_service import search_memories

        results = await search_memories(db, tenant_id, "architecture decision")
        assert len(results) >= 2
        # Memory B (recently recalled) should not be dominated by A (stale popular)
        # Both are relevant — the key is A's 50 recalls don't give it unfair advantage

    async def test_similarity_beats_weight(self, db, tenant_id):
        """P0-4: highly similar + low weight ranks above moderately similar + high weight."""
        # Use very different content to get clearly different similarity scores
        await _insert_memory(
            db,
            tenant_id,
            "kafka consumer lag monitoring alert threshold configuration",
            weight=0.3,  # low weight
        )
        await _insert_memory(
            db,
            tenant_id,
            "general operational procedures for infrastructure management overview",
            weight=0.95,  # high weight
        )

        from core_api.services.memory_service import search_memories

        results = await search_memories(db, tenant_id, "kafka consumer lag monitoring")
        assert len(results) >= 1
        # The highly similar kafka memory should rank first despite low weight
        assert "kafka" in results[0].content

    async def test_entity_boost_with_stopword_filtering(self, db, tenant_id):
        """P0-1 + entity boost: stopwords don't pollute entity matching."""
        # Create entity
        entity = await _insert_entity(db, tenant_id, "kafka cluster")

        # Create memory linked to entity
        mem = await _insert_memory(
            db,
            tenant_id,
            "kafka cluster status healthy all nodes running",
            weight=0.7,
        )
        await _link_memory_entity(db, mem["id"], entity["id"])

        # Create unrelated memory
        await _insert_memory(
            db,
            tenant_id,
            "weather forecast shows clear skies for tomorrow",
            weight=0.7,
        )

        from core_api.services.memory_service import search_memories

        results = await search_memories(db, tenant_id, "kafka cluster status")
        assert len(results) >= 1
        assert "kafka" in results[0].content

    async def test_search_with_all_fixes_combined(self, db, tenant_id):
        """Smoke test: all four P0 fixes working together."""
        now = datetime.now(timezone.utc)

        # Memory 1: old, high weight, lots of stale recalls
        await _insert_memory(
            db,
            tenant_id,
            "redis cache performance tuning guide from last quarter",
            weight=0.95,
            recall_count=100,
            last_recalled_at=now - timedelta(days=60),
            created_at=now - timedelta(days=120),
        )
        # Memory 2: fresh, moderate weight, few recent recalls, entity-linked
        entity = await _insert_entity(db, tenant_id, "redis")
        mem2 = await _insert_memory(
            db,
            tenant_id,
            "redis cache performance dropped to 40% after latest deployment",
            weight=0.6,
            recall_count=3,
            last_recalled_at=now - timedelta(hours=2),
            ts_valid_start=now - timedelta(days=1),
        )
        await _link_memory_entity(db, mem2["id"], entity["id"])

        from core_api.services.memory_service import search_memories

        results = await search_memories(db, tenant_id, "redis cache performance")
        assert len(results) >= 1
        # Memory 2 should win: fresher, recently recalled, entity-boosted
        # Memory 1's high weight and stale recall count shouldn't dominate

    async def test_scored_search_excludes_soft_deleted(self, db, tenant_id):
        # Regression guard for the parallel ``deleted_at IS NULL`` filter
        # sites in core-storage-api/services/postgres_service.py
        # (memory_scored_search at line 857 today). The semantic-dedup
        # path already has its own deleted-row test in
        # tests/test_p2_semantic_dedup.py::test_deleted_memory_doesnt_block;
        # this closes the parallel gap on the scored-search read path.
        #
        # Note: the deleted row is created with ``deleted_at`` ALREADY
        # set, matching the dedup-side test's helper. A post-insert
        # ``UPDATE memories SET deleted_at = ...`` via the per-test
        # ``db`` fixture is invisible to the storage-api ASGI bridge:
        # ``db`` runs the outer transaction in
        # ``join_transaction_mode="create_savepoint"`` (conftest
        # ``db`` fixture), so its commits land at a savepoint inside a
        # never-committed outer transaction — a separate connection
        # (which the storage app's session_factory checks out from the
        # same engine) never sees the change.
        from core_api.clients.storage_client import get_storage_client
        from core_api.services.memory_service import search_memories

        sc = get_storage_client()

        async def _seed(content: str, *, deleted: bool) -> dict:
            payload = {
                "tenant_id": tenant_id,
                "fleet_id": None,
                "agent_id": "scored-search-soft-delete",
                "memory_type": "fact",
                "content": content,
                "weight": 0.7,
                "embedding": fake_embedding(content),
                "content_hash": _hash(tenant_id, None, content),
                "status": "active",
                "visibility": "scope_team",
            }
            if deleted:
                payload["deleted_at"] = datetime.now(timezone.utc).isoformat()
            mem = await sc.create_memory(payload)
            await db.execute(
                text(
                    "UPDATE memories SET search_vector = to_tsvector('english', :content) WHERE id = :id"
                ),
                {"content": content, "id": mem["id"]},
            )
            await db.commit()
            return mem

        deleted_mem = await _seed(
            "kubernetes pod restart troubleshooting guide", deleted=True
        )
        live_mem = await _seed(
            "kubernetes pod restart common causes summary", deleted=False
        )

        results = await search_memories(db, tenant_id, "kubernetes pod restart")

        result_ids = {str(r.id) for r in results}
        assert str(deleted_mem["id"]) not in result_ids, (
            "soft-deleted memory leaked into scored search results"
        )
        assert str(live_mem["id"]) in result_ids, (
            "live memory was not returned by scored search"
        )
