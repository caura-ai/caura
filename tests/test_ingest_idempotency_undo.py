"""Tests for A2 doc-hash idempotency cache + A3 undo endpoint (PR #6).

A2 covers:
- _doc_hash determinism
- ingest_preview returning cached facts when a prior run is found
- Cached preview's run_id matches the prior ingest_run_id
- Cache miss = normal LLM extraction path
- doc_hash echoes on every preview response (cached or fresh)
- ingest_commit stamps doc_hash on memory metadata when request.doc_hash is set

A3 covers _find_prior_ingest_by_doc_hash filtering by tenant + source="ingest"
+ deleted_at IS NULL, but the HTTP-layer undo endpoint test lives separately
once the integration harness is set up. Here we test the building blocks.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core_api.schemas import IngestCommitRequest, IngestFact, IngestRequest
from core_api.services import ingest_service


# ---------------------------------------------------------------------------
# _doc_hash helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_doc_hash_deterministic_for_same_inputs():
    """Same (tenant, content) → same hash. Different content → different."""
    h1 = ingest_service._doc_hash("t1", "Same content here.")
    h2 = ingest_service._doc_hash("t1", "Same content here.")
    h3 = ingest_service._doc_hash("t1", "Different content here.")
    h4 = ingest_service._doc_hash("t2", "Same content here.")  # different tenant
    assert h1 == h2
    assert h1 != h3
    assert h1 != h4  # tenant scoping
    # SHA-256 hex digest = 64 chars
    assert len(h1) == 64


# ---------------------------------------------------------------------------
# A2 cache-hit returns prior facts without an LLM call
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_tenant_config(monkeypatch):
    async def _fake(db, tenant_id):
        return SimpleNamespace(
            enrichment_provider="fake",
            enrichment_enabled=False,
            default_write_mode="fast",
        )

    monkeypatch.setattr(ingest_service, "resolve_config", _fake)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cache_hit_returns_cached_facts_without_llm(
    monkeypatch, fake_tenant_config
):
    """Two prior memories with this doc_hash → preview returns them as cached
    facts with cached=True, the prior run_id, and chunk_ms=0."""
    # Stand up two prior memories as if a previous ingest happened
    prior_run_id = "00000000-1111-2222-3333-444444444444"
    prior_memory = MagicMock()
    prior_memory.content = "The Eiffel Tower is 330 meters tall."
    prior_memory.memory_type = "fact"
    prior_memory.source_uri = "text-input"
    prior_memory.metadata_ = {
        "source": "ingest",
        "ingest_run_id": prior_run_id,
        "doc_hash": "irrelevant",
        "salience": 0.85,
    }

    async def _fake_lookup(db, tenant_id, doc_hash):
        return [prior_memory]

    monkeypatch.setattr(ingest_service, "_find_prior_ingest_by_doc_hash", _fake_lookup)

    # Track that the LLM was NOT called
    llm_called = False

    async def _fake_chunk(text, focus=None, tenant_config=None):
        nonlocal llm_called
        llm_called = True
        return [{"content": "should not be called", "suggested_type": "fact"}]

    monkeypatch.setattr(ingest_service, "_chunk_content", _fake_chunk)

    req = IngestRequest(tenant_id="t1", content="The Eiffel Tower is 330 meters tall.")
    resp = await ingest_service.ingest_preview(db=None, request=req)

    assert resp["cached"] is True
    assert resp["run_id"] == prior_run_id
    assert resp["chunk_ms"] == 0
    assert len(resp["facts"]) == 1
    assert resp["facts"][0]["content"] == prior_memory.content
    assert resp["facts"][0]["salience"] == 0.85
    assert not llm_called, "LLM should not be invoked on a cache hit"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cache_miss_runs_llm_normally(monkeypatch, fake_tenant_config):
    """No prior memories → cache miss → LLM runs, response carries doc_hash
    so the next caller can echo it back to commit for future cache hits."""

    async def _no_cache(db, tenant_id, doc_hash):
        return []

    monkeypatch.setattr(ingest_service, "_find_prior_ingest_by_doc_hash", _no_cache)

    async def _fake_chunk(text, focus=None, tenant_config=None):
        return [
            {
                "content": "Mercury orbits in 88 days.",
                "suggested_type": "fact",
                "salience": 0.9,
            }
        ]

    monkeypatch.setattr(ingest_service, "_chunk_content", _fake_chunk)

    req = IngestRequest(tenant_id="t1", content="Mercury orbits the Sun every 88 days.")
    resp = await ingest_service.ingest_preview(db=None, request=req)

    assert resp.get("cached") is not True
    assert "doc_hash" in resp
    assert resp["doc_hash"] == ingest_service._doc_hash(
        "t1", "Mercury orbits the Sun every 88 days."
    )
    assert len(resp["facts"]) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cache_hit_picks_only_most_recent_run(monkeypatch):
    """If memories from two prior runs share a doc_hash, only the newest run's
    memories are returned. ``_find_prior_ingest_by_doc_hash`` does the filtering;
    we exercise the same logic by constructing a mock DB result."""
    older_mem = MagicMock()
    older_mem.metadata_ = {
        "source": "ingest",
        "ingest_run_id": "older-run-id",
        "doc_hash": "h",
    }
    older_mem.content = "older fact"
    older_mem.memory_type = "fact"
    older_mem.source_uri = "text-input"
    newer_mem = MagicMock()
    newer_mem.metadata_ = {
        "source": "ingest",
        "ingest_run_id": "newer-run-id",
        "doc_hash": "h",
    }
    newer_mem.content = "newer fact"
    newer_mem.memory_type = "fact"
    newer_mem.source_uri = "text-input"

    # _find_prior_ingest_by_doc_hash orders by created_at desc and filters
    # to the newest run_id. Simulate the inputs and verify the filter.
    fake_db = MagicMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [newer_mem, older_mem]
    fake_db.execute = AsyncMock(return_value=mock_result)
    facts = await ingest_service._find_prior_ingest_by_doc_hash(fake_db, "t1", "h")
    # Only the memory tagged with newer-run-id is kept
    assert facts == [newer_mem]


# ---------------------------------------------------------------------------
# A2: commit stamps doc_hash on metadata when caller echoes it
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_commit_stamps_doc_hash_in_metadata_when_echoed(monkeypatch):
    """When the caller sends doc_hash back to commit, every written memory's
    metadata.doc_hash is populated. This is what enables future A2 hits."""
    writes: list = []

    async def _fake_resolve_config(db, tenant_id):
        return SimpleNamespace(
            enrichment_provider="fake",
            enrichment_enabled=False,
            default_write_mode="fast",
        )

    async def _fake_create(db, data):
        writes.append(data)
        return SimpleNamespace(id="00000000-0000-0000-0000-000000000001")

    monkeypatch.setattr(ingest_service, "resolve_config", _fake_resolve_config)
    monkeypatch.setattr(ingest_service, "create_memory", _fake_create)
    sc_mock = MagicMock()
    sc_mock.bulk_find_by_content_hashes = AsyncMock(return_value={})
    monkeypatch.setattr(ingest_service, "get_storage_client", lambda: sc_mock)

    req = IngestCommitRequest(
        tenant_id="t1",
        doc_hash="abc123" * 10,  # 60 chars, plausible-shaped digest
        facts=[
            IngestFact(content="Real fact A about something.", suggested_type="fact"),
            IngestFact(content="Real fact B about something.", suggested_type="fact"),
        ],
    )
    result = await ingest_service.ingest_commit(db=None, request=req)

    assert result["memories_created"] == 2
    for w in writes:
        assert w.metadata["doc_hash"] == "abc123" * 10


@pytest.mark.unit
@pytest.mark.asyncio
async def test_commit_does_not_stamp_doc_hash_when_omitted(monkeypatch):
    """Backward-compat: a commit without doc_hash leaves metadata.doc_hash unset.
    Pre-PR #6 callers continue to work; their memories simply won't seed the
    A2 cache."""
    writes: list = []

    async def _fake_resolve_config(db, tenant_id):
        return SimpleNamespace(
            enrichment_provider="fake",
            enrichment_enabled=False,
            default_write_mode="fast",
        )

    async def _fake_create(db, data):
        writes.append(data)
        return SimpleNamespace(id="00000000-0000-0000-0000-000000000001")

    monkeypatch.setattr(ingest_service, "resolve_config", _fake_resolve_config)
    monkeypatch.setattr(ingest_service, "create_memory", _fake_create)
    sc_mock = MagicMock()
    sc_mock.bulk_find_by_content_hashes = AsyncMock(return_value={})
    monkeypatch.setattr(ingest_service, "get_storage_client", lambda: sc_mock)

    req = IngestCommitRequest(
        tenant_id="t1",
        facts=[
            IngestFact(
                content="Real fact without doc_hash echo.", suggested_type="fact"
            )
        ],
    )
    await ingest_service.ingest_commit(db=None, request=req)

    assert len(writes) == 1
    assert "doc_hash" not in writes[0].metadata


@pytest.mark.unit
@pytest.mark.asyncio
async def test_commit_stamps_salience_when_present_on_ingestfact(monkeypatch):
    """If preview returned facts with salience, the commit-side round-trip
    persists them on memory.metadata.salience (needed for A2-cache to
    surface salience on the cached preview)."""
    writes: list = []

    async def _fake_resolve_config(db, tenant_id):
        return SimpleNamespace(
            enrichment_provider="fake",
            enrichment_enabled=False,
            default_write_mode="fast",
        )

    async def _fake_create(db, data):
        writes.append(data)
        return SimpleNamespace(id="00000000-0000-0000-0000-000000000001")

    monkeypatch.setattr(ingest_service, "resolve_config", _fake_resolve_config)
    monkeypatch.setattr(ingest_service, "create_memory", _fake_create)
    sc_mock = MagicMock()
    sc_mock.bulk_find_by_content_hashes = AsyncMock(return_value={})
    monkeypatch.setattr(ingest_service, "get_storage_client", lambda: sc_mock)

    req = IngestCommitRequest(
        tenant_id="t1",
        facts=[
            IngestFact(
                content="Fact with salience attached.",
                suggested_type="fact",
                salience=0.85,
            )
        ],
    )
    await ingest_service.ingest_commit(db=None, request=req)
    assert writes[0].metadata["salience"] == 0.85
