"""Tests for the strong-mode + parallel + pre-loop-dedup changes in PR #2.

Covers:
- P1.3 ``MemoryCreate.write_mode == "strong"`` on every ingested fact
- P1.3 Concurrent execution via ``asyncio.Semaphore(_COMMIT_CONCURRENCY)``
- P1.3 ``resolve_config`` is pre-warmed once before the loop
- P1.4 Pre-loop ``bulk_find_by_content_hashes`` filters dups before LLM
- P1.4 Dedup query failure falls through gracefully
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core_api.schemas import IngestCommitRequest, IngestFact
from core_api.services import ingest_service


def _request(tenant_id: str = "t1", *facts: str, **kwargs) -> IngestCommitRequest:
    """Build an IngestCommitRequest with N text facts (suggested_type=fact)."""
    return IngestCommitRequest(
        tenant_id=tenant_id,
        facts=[IngestFact(content=c, suggested_type="fact") for c in facts],
        **kwargs,
    )


@pytest.fixture
def captured(monkeypatch):
    """Patch the external collaborators of ``ingest_commit`` and capture all calls.

    Returns a SimpleNamespace exposing:
      - ``writes``: list[MemoryCreate]   facts passed to create_memory
      - ``bulk_find_calls``: list[(tenant_id, hashes)]
      - ``resolve_config_calls``: list[tenant_id]
      - ``bulk_find_result``: dict[str, dict]  what bulk_find_by_content_hashes returns (configurable)
      - ``write_delay_ms``: per-write artificial latency
      - ``write_409_for``: set[content_hash]   simulate 409 from create_memory for those facts
    """
    state = SimpleNamespace(
        writes=[],
        bulk_find_calls=[],
        resolve_config_calls=[],
        bulk_find_result={},
        write_delay_ms=0,
        write_409_for=set(),
    )

    async def fake_resolve_config(db, tenant_id):
        state.resolve_config_calls.append(tenant_id)
        return SimpleNamespace(
            enrichment_provider="fake",
            enrichment_enabled=False,
            default_write_mode="fast",
        )

    async def fake_bulk_find(tenant_id, hashes):
        state.bulk_find_calls.append((tenant_id, list(hashes)))
        # Mimic the storage_client contract: dict[content_hash, {"id": str, ...}]
        return {
            h: {"id": "x", "client_request_id": None}
            for h in hashes
            if h in state.bulk_find_result
        }

    async def fake_create_memory(db, data):
        state.writes.append(data)
        if state.write_delay_ms:
            await asyncio.sleep(state.write_delay_ms / 1000.0)
        # Simulate 409 from inside create_memory when configured
        from fastapi import HTTPException

        from core_api.services.memory_service import _content_hash as ch_fn

        h = ch_fn(data.tenant_id, data.fleet_id, data.content)
        if h in state.write_409_for:
            raise HTTPException(status_code=409, detail="duplicate")
        return SimpleNamespace(id="00000000-0000-0000-0000-000000000000")

    # Patch the symbols *as imported into ingest_service*
    monkeypatch.setattr(ingest_service, "resolve_config", fake_resolve_config)
    monkeypatch.setattr(ingest_service, "create_memory", fake_create_memory)
    mock_sc = MagicMock()
    mock_sc.bulk_find_by_content_hashes = AsyncMock(side_effect=fake_bulk_find)
    monkeypatch.setattr(ingest_service, "get_storage_client", lambda: mock_sc)
    return state


# ---------------------------------------------------------------------------
# P1.3 — strong mode
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_every_write_uses_strong_mode(captured):
    """All ingest writes set write_mode='strong'."""
    req = _request("t1", "fact one", "fact two", "fact three")
    await ingest_service.ingest_commit(db=None, request=req)

    assert len(captured.writes) == 3
    for mc in captured.writes:
        assert mc.write_mode == "strong"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_metadata_carries_run_id_and_ingest_source(captured):
    req = _request("t1", "fact one")
    result = await ingest_service.ingest_commit(db=None, request=req)

    assert len(captured.writes) == 1
    md = captured.writes[0].metadata
    assert md["source"] == "ingest"
    assert md["ingest_run_id"] == result["run_id"]


# ---------------------------------------------------------------------------
# P1.3 — pre-warm tenant config
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_config_called_once_before_loop(captured):
    """``resolve_config`` runs exactly once at the top — pre-warming the cache
    so the per-fact pipeline doesn't race on the shared session."""
    req = _request("t1", "f1", "f2", "f3", "f4", "f5")
    await ingest_service.ingest_commit(db=None, request=req)

    assert captured.resolve_config_calls == ["t1"]


# ---------------------------------------------------------------------------
# P1.3 — concurrency via Semaphore
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_writes_run_in_parallel_under_semaphore(captured):
    """8 facts × 100ms simulated write should complete in well under 800ms
    when Semaphore(4) is doing its job — wall clock ~200ms (2 batches of 4)."""
    captured.write_delay_ms = 100
    req = _request("t1", *(f"fact {i}" for i in range(8)))

    t0 = time.perf_counter()
    result = await ingest_service.ingest_commit(db=None, request=req)
    elapsed = time.perf_counter() - t0

    assert result["memories_created"] == 8
    # Serial would be 800ms; with Semaphore(4) it's 2 waves × 100ms ≈ 200ms.
    # Generous bound to keep the test stable on CI noise.
    assert elapsed < 0.5, f"Expected parallel speedup but ran in {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# P1.4 — pre-loop content-hash dedup
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pre_loop_dedup_skips_known_hashes_before_writes(captured):
    """Dup facts (whose hashes are already in storage) never reach create_memory.

    This is the main bug P1.4 fixes — under strong-mode, every dup that
    reaches create_memory pays a full LLM enrichment call before the 409
    rejection. Pre-loop dedup is the cost-saving gate.
    """
    from core_api.services.memory_service import _content_hash

    # Pre-populate the bulk_find result with 2 of the 5 facts' hashes
    facts = ["alpha", "beta", "gamma", "delta", "epsilon"]
    hashes = [_content_hash("t1", None, c) for c in facts]
    captured.bulk_find_result = {hashes[1], hashes[3]}  # "beta" and "delta" are dups

    req = _request("t1", *facts)
    result = await ingest_service.ingest_commit(db=None, request=req)

    assert result["facts_extracted"] == 5
    assert result["memories_created"] == 3
    assert result["skipped_duplicates"] == 2
    # Only the 3 non-dup facts should have reached create_memory
    written_contents = {mc.content for mc in captured.writes}
    assert written_contents == {"alpha", "gamma", "epsilon"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pre_loop_dedup_calls_bulk_find_with_all_hashes(captured):
    """The dedup query receives every incoming fact's hash, not a subset."""
    req = _request("t1", "f1", "f2", "f3")
    await ingest_service.ingest_commit(db=None, request=req)

    assert len(captured.bulk_find_calls) == 1
    tenant_id, hashes = captured.bulk_find_calls[0]
    assert tenant_id == "t1"
    assert len(hashes) == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pre_loop_dedup_failure_falls_through_to_per_fact_path(
    captured, monkeypatch
):
    """If bulk_find_by_content_hashes raises, ingest_commit must still write
    the facts (correctness over speed). The per-fact 409 path still dedupes."""

    async def boom(*args, **kwargs):
        raise RuntimeError("storage flaky")

    captured_sc = MagicMock()
    captured_sc.bulk_find_by_content_hashes = AsyncMock(side_effect=boom)
    monkeypatch.setattr(ingest_service, "get_storage_client", lambda: captured_sc)

    req = _request("t1", "f1", "f2", "f3")
    result = await ingest_service.ingest_commit(db=None, request=req)

    assert result["memories_created"] == 3
    assert result["skipped_duplicates"] == 0


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_empty_facts_list_is_a_noop(captured):
    """No facts → no DB writes, no bulk_find, but still returns a valid run_id."""
    req = _request("t1")  # no facts
    result = await ingest_service.ingest_commit(db=None, request=req)

    assert result["facts_extracted"] == 0
    assert result["memories_created"] == 0
    assert result["skipped_duplicates"] == 0
    assert result["run_id"]  # auto-minted UUID
    assert captured.writes == []
    assert captured.bulk_find_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_in_loop_409_still_counts_as_skipped(captured):
    """create_memory's own 409 (race vs concurrent writer) still increments
    skipped_duplicates after passing the pre-loop dedup."""
    from core_api.services.memory_service import _content_hash

    facts = ["only-fact"]
    captured.write_409_for = {_content_hash("t1", None, facts[0])}

    req = _request("t1", *facts)
    result = await ingest_service.ingest_commit(db=None, request=req)

    assert result["memories_created"] == 0
    assert result["skipped_duplicates"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_url_provenance_request_level_wins(captured):
    """Caller-supplied request.url wins for back-compat with the dashboard."""
    req = _request("t1", "f1", url="https://example.com/doc.md")
    await ingest_service.ingest_commit(db=None, request=req)

    assert captured.writes[0].source_uri == "https://example.com/doc.md"
    assert captured.writes[0].metadata["ingest_url"] == "https://example.com/doc.md"


# ---------------------------------------------------------------------------
# PR #3 — P1.2 per-fact source_uri precedence
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_per_fact_source_uri_used_when_no_request_url(captured):
    """If the caller round-trips preview output without re-passing url, each
    fact's own ``source_uri`` (stamped by preview) is honored."""
    from core_api.schemas import IngestCommitRequest, IngestFact

    req = IngestCommitRequest(
        tenant_id="t1",
        facts=[
            IngestFact(
                content="Fact A from URL",
                suggested_type="fact",
                source_uri="https://example.com/doc1",
            ),
            IngestFact(
                content="Fact B from URL",
                suggested_type="fact",
                source_uri="https://example.com/doc1",
            ),
        ],
    )
    await ingest_service.ingest_commit(db=None, request=req)

    for w in captured.writes:
        assert w.source_uri == "https://example.com/doc1"
        assert w.metadata["ingest_url"] == "https://example.com/doc1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_request_url_overrides_fact_source_uri(captured):
    """If both request.url and fact.source_uri are set, request.url wins
    (dashboard back-compat — the form's url state is authoritative)."""
    from core_api.schemas import IngestCommitRequest, IngestFact

    req = IngestCommitRequest(
        tenant_id="t1",
        url="https://override.example/canonical",
        facts=[
            IngestFact(
                content="Fact",
                suggested_type="fact",
                source_uri="https://stamped-by-preview/old",
            ),
        ],
    )
    await ingest_service.ingest_commit(db=None, request=req)

    assert captured.writes[0].source_uri == "https://override.example/canonical"
    assert (
        captured.writes[0].metadata["ingest_url"]
        == "https://override.example/canonical"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_text_input_fallback_when_neither_set(captured):
    """Neither request.url nor fact.source_uri → 'text-input'."""
    req = _request("t1", "plain content fact")  # _request doesn't set source_uri
    await ingest_service.ingest_commit(db=None, request=req)

    assert captured.writes[0].source_uri == "text-input"
    assert captured.writes[0].metadata["ingest_url"] is None


# ---------------------------------------------------------------------------
# PR #3 — P1.E suggested_type validation at commit
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_invalid_suggested_type_raises_422_before_any_work(captured):
    """A forged or garbage suggested_type → 422 with the offending value, BEFORE
    we touch the dedup query or create_memory. Pre-PR #3 this leaked all the
    way to MemoryCreate's enum validation and surfaced as a 500."""
    from fastapi import HTTPException

    from core_api.schemas import IngestCommitRequest, IngestFact

    req = IngestCommitRequest(
        tenant_id="t1",
        facts=[
            IngestFact(content="Real fact 1", suggested_type="fact"),
            IngestFact(content="Garbage", suggested_type="🦀garbage"),
            IngestFact(content="Real fact 2", suggested_type="decision"),
        ],
    )
    with pytest.raises(HTTPException) as exc:
        await ingest_service.ingest_commit(db=None, request=req)

    assert exc.value.status_code == 422
    assert "🦀garbage" in exc.value.detail
    assert "[1]" in exc.value.detail  # index 1 is the bad one
    # No writes happened
    assert captured.writes == []
    assert captured.bulk_find_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_valid_suggested_types_pass_through(captured):
    """All known MEMORY_TYPES pass the gate."""
    from core_api.constants import MEMORY_TYPES
    from core_api.schemas import IngestCommitRequest, IngestFact

    req = IngestCommitRequest(
        tenant_id="t1",
        facts=[IngestFact(content=f"Fact {t}", suggested_type=t) for t in MEMORY_TYPES],
    )
    result = await ingest_service.ingest_commit(db=None, request=req)
    assert result["memories_created"] == len(MEMORY_TYPES)
