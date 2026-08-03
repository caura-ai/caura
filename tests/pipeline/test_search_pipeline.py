"""Integration tests: search pipeline path vs legacy path produce equivalent output.

These tests require a running PostgreSQL instance (same as other integration tests).
They exercise both paths with identical inputs and compare the MemoryOut results.
"""

import uuid

import pytest
from sqlalchemy import text

from core_api.schemas import MemoryCreate, MemoryOut

TENANT_ID = f"test-search-pipe-{uuid.uuid4().hex[:8]}"
FLEET_ID = "test-fleet"
AGENT_ID = "test-agent"


_SEED_CONTENTS = [
    "The quick brown fox jumped over the lazy dog on a sunny afternoon in the park near downtown.",
    "Alice prefers dark roast coffee every morning before her standup meeting at nine o'clock sharp.",
    "The quarterly budget review is scheduled for next Friday with the entire finance department attending.",
    "Bob mentioned he is allergic to peanuts and tree nuts, which is important for team lunch orders.",
    "The new deployment pipeline uses GitHub Actions with staging and production environments configured.",
]


async def _seed_memories(db, count: int = 3) -> list[MemoryOut]:
    """Insert test memories via the legacy write path and return them."""
    from core_api.services.memory_service import create_memory

    # Use a unique tenant per call to avoid cross-test dedup collisions
    tid = f"test-search-pipe-{uuid.uuid4().hex[:8]}"

    results = []
    for i in range(min(count, len(_SEED_CONTENTS))):
        data = MemoryCreate(
            tenant_id=tid,
            fleet_id=FLEET_ID,
            agent_id=AGENT_ID,
            content=_SEED_CONTENTS[i],
            persist=True,
            entity_links=[],
        )
        result = await create_memory(data)
        results.append(result)
    return results


# ---------------------------------------------------------------------------
# Unit tests (no DB required)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_search_profile_defaults():
    """ResolveSearchProfile sets search_params with default constants."""
    from unittest.mock import AsyncMock

    from core_api.constants import MIN_SEARCH_SIMILARITY
    from core_api.pipeline.context import PipelineContext
    from core_api.pipeline.steps.search.resolve_search_profile import (
        ResolveSearchProfile,
    )

    ctx = PipelineContext(
                data={
            "query": "test query",
            "top_k": 5,
            "search_profile": None,
        },
    )
    step = ResolveSearchProfile()
    await step.execute(ctx)

    sp = ctx.data["search_params"]
    assert sp["top_k"] == 5
    assert sp["min_similarity"] == MIN_SEARCH_SIMILARITY
    assert "fts_weight" in sp
    assert "freshness_floor" in sp


@pytest.mark.asyncio
async def test_extract_temporal_hint_today():
    """ExtractTemporalHint detects 'today' as 1-day window."""
    from datetime import timedelta
    from unittest.mock import AsyncMock

    from core_api.pipeline.context import PipelineContext
    from core_api.pipeline.steps.search.extract_temporal_hint import (
        ExtractTemporalHint,
    )

    ctx = PipelineContext(
                data={"query": "what happened today"},
    )
    step = ExtractTemporalHint()
    await step.execute(ctx)

    assert ctx.data["temporal_window"] == timedelta(days=1)


@pytest.mark.asyncio
async def test_extract_temporal_hint_none():
    """ExtractTemporalHint returns None for non-temporal queries."""
    from unittest.mock import AsyncMock

    from core_api.pipeline.context import PipelineContext
    from core_api.pipeline.steps.search.extract_temporal_hint import (
        ExtractTemporalHint,
    )

    ctx = PipelineContext(
                data={"query": "favorite color"},
    )
    step = ExtractTemporalHint()
    await step.execute(ctx)

    assert ctx.data["temporal_window"] is None
    assert ctx.data["date_range_filter"] is None


@pytest.mark.asyncio
async def test_extract_temporal_hint_sets_date_range():
    """ExtractTemporalHint sets date_range_filter for temporal queries."""
    from unittest.mock import AsyncMock

    from core_api.pipeline.context import PipelineContext
    from core_api.pipeline.steps.search.extract_temporal_hint import (
        ExtractTemporalHint,
    )

    ctx = PipelineContext(
                data={"query": "what happened two months ago"},
    )
    step = ExtractTemporalHint()
    await step.execute(ctx)

    assert ctx.data["date_range_filter"] is not None
    assert "start_date" in ctx.data["date_range_filter"]
    assert "end_date" in ctx.data["date_range_filter"]


@pytest.mark.asyncio
async def test_extract_temporal_hint_uses_valid_at_as_reference():
    """ExtractTemporalHint uses valid_at as reference datetime when available."""
    from datetime import datetime
    from unittest.mock import AsyncMock

    from core_api.pipeline.context import PipelineContext
    from core_api.pipeline.steps.search.extract_temporal_hint import (
        ExtractTemporalHint,
    )

    ref = datetime(2026, 4, 14, 12, 0, 0)
    ctx = PipelineContext(
                data={"query": "notes from two weeks ago", "valid_at": ref},
    )
    step = ExtractTemporalHint()
    await step.execute(ctx)

    dr = ctx.data["date_range_filter"]
    assert dr is not None
    # 2 weeks = 14 days → target = 2026-03-31, range ±1 (week unit)
    assert dr["start_date"] == "2026-03-30"
    assert dr["end_date"] == "2026-04-01"


@pytest.mark.asyncio
async def test_post_filter_results():
    """PostFilterResults filters rows below min_similarity."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from core_api.pipeline.context import PipelineContext
    from core_api.pipeline.steps.search.post_filter_results import PostFilterResults

    rows = [
        SimpleNamespace(
            vec_sim=0.8, Memory=None, score=0.7, similarity=0.7, entity_links=[]
        ),
        SimpleNamespace(
            vec_sim=0.3, Memory=None, score=0.2, similarity=0.2, entity_links=[]
        ),
        SimpleNamespace(
            vec_sim=0.6, Memory=None, score=0.5, similarity=0.5, entity_links=[]
        ),
    ]
    ctx = PipelineContext(
                data={
            "raw_rows": rows,
            "search_params": {"min_similarity": 0.5},
        },
    )
    step = PostFilterResults()
    await step.execute(ctx)

    assert len(ctx.data["filtered_rows"]) == 2
    assert all(float(r.vec_sim) >= 0.5 for r in ctx.data["filtered_rows"])


@pytest.mark.asyncio
async def test_load_and_serialize_uses_preloaded_entity_links():
    """LoadAndSerialize reads entity_links from rows instead of querying DB."""
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from core_api.schemas import EntityLinkOut
    from core_api.pipeline.context import PipelineContext
    from core_api.pipeline.steps.search.load_and_serialize import LoadAndSerialize

    mem = MagicMock()
    mem.id = uuid.uuid4()
    mem.tenant_id = "t1"
    mem.fleet_id = "f1"
    mem.agent_id = "a1"
    mem.agent_display_name = None
    mem.memory_type = "fact"
    mem.title = "Test"
    mem.content = "test content"
    mem.weight = 0.5
    mem.source_uri = None
    mem.run_id = None
    mem.metadata_ = None
    mem.created_at = datetime.now(timezone.utc)
    mem.expires_at = None
    mem.subject_entity_id = None
    mem.predicate = None
    mem.object_value = None
    mem.ts_valid_start = None
    mem.ts_valid_end = None
    mem.status = "active"
    mem.visibility = "scope_team"
    mem.recall_count = 0
    mem.last_recalled_at = None
    mem.supersedes_id = None

    entity_link = EntityLinkOut(entity_id=uuid.uuid4(), role="subject")
    row = SimpleNamespace(
        Memory=mem,
        score=0.85,
        similarity=0.8,
        vec_sim=0.9,
        entity_links=[entity_link],
    )

    mock_db = AsyncMock()
    ctx = PipelineContext(
                data={"filtered_rows": [row]},
    )
    step = LoadAndSerialize()
    await step.execute(ctx)

    # DB should NOT have been called (entity links are pre-loaded)
    mock_db.execute.assert_not_called()

    results = ctx.data["results"]
    assert len(results) == 1
    assert len(results[0].entity_links) == 1
    # similarity must be the raw vector cosine (vec_sim=0.9), NOT the ranking
    # composite (score=0.85) or the vec/FTS blend (similarity=0.8) — see F-14.
    assert results[0].similarity == 0.9
    assert results[0].entity_links[0].entity_id == entity_link.entity_id


@pytest.mark.asyncio
async def test_track_recalls_fire_and_forget():
    """TrackRecalls spawns a background task instead of awaiting on the request session."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock, patch

    from core_api.pipeline.context import PipelineContext
    from core_api.pipeline.steps.search.track_recalls import TrackRecalls

    mem1 = MagicMock()
    mem1.id = uuid.uuid4()
    mem2 = MagicMock()
    mem2.id = uuid.uuid4()

    rows = [
        SimpleNamespace(Memory=mem1),
        SimpleNamespace(Memory=mem2),
    ]

    mock_db = AsyncMock()
    ctx = PipelineContext(
                # caller_agent_id present → genuine agent recall, so recall_count
                # is bumped (agentless recalls are skipped; see track_recalls).
                data={"filtered_rows": rows, "caller_agent_id": "test-agent"},
    )

    with patch("core_api.pipeline.steps.search.track_recalls.track_task") as mock_track:
        step = TrackRecalls()
        await step.execute(ctx)

        # track_task should have been called with a coroutine
        mock_track.assert_called_once()
        # Close the coroutine to avoid RuntimeWarning
        coro = mock_track.call_args[0][0]
        coro.close()

    # The request DB session should NOT have been used
    mock_db.execute.assert_not_called()
    mock_db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_track_recalls_background_routes_to_storage():
    """The background task routes the recall bump through the storage client
    with stringified memory ids (no direct DB session)."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from core_api.pipeline.steps.search.track_recalls import _track_recalls_background

    ids = [uuid.uuid4(), uuid.uuid4()]

    sc = MagicMock()
    sc.increment_recall = AsyncMock(return_value=2)
    with patch(
        "core_api.pipeline.steps.search.track_recalls.get_storage_client",
        return_value=sc,
    ):
        await _track_recalls_background(ids)

    sc.increment_recall.assert_awaited_once_with([str(ids[0]), str(ids[1])])


# ---------------------------------------------------------------------------
# Fix A — Pipeline failure surfaces original error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_failure_includes_error_detail():
    """Pipeline failure HTTPException includes the original error message."""
    from unittest.mock import AsyncMock

    from core_api.pipeline.context import PipelineContext
    from core_api.pipeline.runner import Pipeline
    from core_api.pipeline.step import StepOutcome

    class FailingStep:
        @property
        def name(self):
            return "failing_step"

        async def execute(self, ctx):
            raise ValueError("something broke in scoring")

    ctx = PipelineContext(data={})
    pipeline = Pipeline("test", [FailingStep()])
    result = await pipeline.run(ctx)

    assert result.failed is True
    # Verify the error is stored in the step result
    failed = [s for s in result.steps if s.outcome == StepOutcome.FAILED]
    assert len(failed) == 1
    assert "something broke in scoring" in str(failed[0].error)


@pytest.mark.asyncio
async def test_search_pipeline_failure_logs_error_not_in_detail():
    """_search_memories_pipeline logs the error but does not leak it in the HTTP detail."""
    from unittest.mock import AsyncMock, patch

    from fastapi import HTTPException

    from core_api.pipeline.runner import Pipeline

    # Create a pipeline that fails
    class FailingStep:
        @property
        def name(self):
            return "failing_step"

        async def execute(self, ctx):
            raise ValueError("test error detail")

    with (
        patch(
            "core_api.pipeline.compositions.search.build_search_pipeline",
            return_value=Pipeline("search", [FailingStep()]),
        ),
        patch("core_api.services.memory_service.logger") as mock_logger,
    ):
        from core_api.services.memory_service import _search_memories_pipeline

        with pytest.raises(HTTPException) as exc_info:
            await _search_memories_pipeline(                tenant_id="t1",
                query="test",
            )

        assert exc_info.value.status_code == 500
        # Error detail must NOT leak internal error messages
        assert "test error detail" not in exc_info.value.detail
        assert exc_info.value.detail == "Search pipeline failed unexpectedly"
        # But the error IS logged server-side
        mock_logger.error.assert_called_once()
        log_args = mock_logger.error.call_args
        assert "test error detail" in str(log_args)


# ---------------------------------------------------------------------------
# Fix D — Parallel embed timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parallel_embed_gather_has_timeout():
    """ParallelEmbedAndEntityBoost has a timeout on asyncio.gather."""
    import asyncio
    from unittest.mock import AsyncMock, patch

    from fastapi import HTTPException

    from core_api.pipeline.context import PipelineContext
    from core_api.pipeline.steps.search.parallel_embed_entity_boost import (
        ParallelEmbedAndEntityBoost,
    )

    ctx = PipelineContext(
                data={
            "query": "test",
            "tenant_id": "t1",
            "tenant_config": None,
            "search_params": {"graph_max_hops": 2},
            "graph_expand": True,
        },
    )

    async def slow_embed(*args, **kwargs):
        await asyncio.sleep(20)
        return [0.0] * 1536

    async def slow_boost(*args, **kwargs):
        await asyncio.sleep(20)
        return ([], {})

    with (
        patch(
            "core_api.pipeline.steps.search.parallel_embed_entity_boost._get_or_cache_embedding",
            side_effect=slow_embed,
        ),
        patch(
            "core_api.pipeline.steps.search.parallel_embed_entity_boost._entity_boost_via_storage",
            side_effect=slow_boost,
        ),
    ):
        step = ParallelEmbedAndEntityBoost()
        with pytest.raises(HTTPException) as exc_info:
            await asyncio.wait_for(step.execute(ctx), timeout=17.0)

        assert exc_info.value.status_code == 504


# ---------------------------------------------------------------------------
# Integration tests (require PostgreSQL)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_search_returns_results(db):
    """Pipeline search path returns MemoryOut results for seeded memories."""
    from core_api.services import memory_service

    seeded = await _seed_memories(db, count=2)
    tid = seeded[0].tenant_id

    original = memory_service._USE_PIPELINE_SEARCH
    memory_service._USE_PIPELINE_SEARCH = True
    try:
        from core_api.services.memory_service import search_memories

        results = await search_memories(tenant_id=tid,
            query="quick brown fox",
            fleet_ids=[FLEET_ID],
            caller_agent_id=AGENT_ID,
        )
        assert isinstance(results, list)
        assert len(results) > 0
        assert all(isinstance(r, MemoryOut) for r in results)
    finally:
        memory_service._USE_PIPELINE_SEARCH = original


@pytest.mark.asyncio
async def test_legacy_search_returns_results(db):
    """Legacy search path returns results (baseline)."""
    from core_api.services import memory_service

    seeded = await _seed_memories(db, count=2)
    tid = seeded[0].tenant_id

    original = memory_service._USE_PIPELINE_SEARCH
    memory_service._USE_PIPELINE_SEARCH = False
    try:
        from core_api.services.memory_service import search_memories

        results = await search_memories(tenant_id=tid,
            query="quick brown fox",
            fleet_ids=[FLEET_ID],
            caller_agent_id=AGENT_ID,
        )
        assert isinstance(results, list)
        assert len(results) > 0
    finally:
        memory_service._USE_PIPELINE_SEARCH = original


@pytest.mark.asyncio
async def test_search_pipeline_equivalence(db):
    """Pipeline and legacy paths produce equivalent results (order, scores to 4 decimals, entity links)."""
    from core_api.services import memory_service
    from core_api.services.memory_service import (
        _search_memories_legacy,
        _search_memories_pipeline,
    )

    seeded = await _seed_memories(db, count=3)
    tid = seeded[0].tenant_id

    query = "quick brown fox sunny afternoon"
    # Disable recall_boost so the legacy path's recall tracking side-effect
    # (incrementing recall_count) doesn't change scores for the pipeline run.
    kwargs = {
        "tenant_id": tid,
        "query": query,
        "fleet_ids": [FLEET_ID],
        "caller_agent_id": AGENT_ID,
        "recall_boost": False,
    }

    memory_service._USE_PIPELINE_SEARCH = False
    legacy_results = await _search_memories_legacy(**kwargs)

    memory_service._USE_PIPELINE_SEARCH = True
    pipeline_results = await _search_memories_pipeline(**kwargs)
    memory_service._USE_PIPELINE_SEARCH = False

    assert len(legacy_results) == len(pipeline_results), (
        f"Result count mismatch: legacy={len(legacy_results)}, pipeline={len(pipeline_results)}"
    )

    for i, (leg, pip) in enumerate(zip(legacy_results, pipeline_results)):
        assert leg.id == pip.id, f"Row {i}: ID mismatch {leg.id} != {pip.id}"
        assert leg.similarity == pip.similarity, (
            f"Row {i}: score mismatch {leg.similarity} != {pip.similarity}"
        )
        leg_links = sorted([(el.entity_id, el.role) for el in leg.entity_links])
        pip_links = sorted([(el.entity_id, el.role) for el in pip.entity_links])
        assert leg_links == pip_links, f"Row {i}: entity_links mismatch"


@pytest.mark.asyncio
async def test_search_pipeline_empty_results(db):
    """Pipeline search returns empty list for no-match query."""
    from core_api.services import memory_service

    original = memory_service._USE_PIPELINE_SEARCH
    memory_service._USE_PIPELINE_SEARCH = True
    try:
        from core_api.services.memory_service import search_memories

        results = await search_memories(tenant_id=f"nonexistent-tenant-{uuid.uuid4().hex[:8]}",
            query="zzz no match zzz",
        )
        assert results == []
    finally:
        memory_service._USE_PIPELINE_SEARCH = original


# ---------------------------------------------------------------------------
# Recall of a memory whose embedding backfill has not landed yet
# (caura-memclaw#687)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("use_pipeline", [True, False], ids=["pipeline", "legacy"])
async def test_recall_returns_memory_with_pending_embedding(db, monkeypatch, use_pipeline):
    """A memory must be recallable before its embedding backfill lands.

    Production defers the embedding: under ``deployment_mode=deferred`` the row
    is stored with ``embedding IS NULL`` and ``metadata.embedding_pending=True``,
    and a worker back-fills it seconds (prod) to minutes (staging) later. Three
    pieces of machinery exist so the row stays discoverable in that window —
    CAURA-594's admission predicate ``or_(embedding IS NOT NULL, fts_guard)``,
    CAURA-679's FTS-only ``similarity`` for NULL-embedding rows, and
    PostFilterResults exempting them from the ``vec_sim`` gate.

    Nothing asserted that end to end, which is the gap #687 shipped through:
    against the live control plane a row that is present and whose content is
    byte-identical to the query is not returned until it has been embedded.
    core-storage-api's own integration test covers the storage layer but POSTs
    ``embedding=None`` straight to core-storage-api, bypassing core-api, and no
    test anywhere pairs a pending embedding with a recall.

    Both search implementations are exercised: ``search_memories`` dispatches on
    ``_USE_PIPELINE_SEARCH``, so covering only the default would leave the other
    path unpinned — and a divergence between them localises the defect.
    """
    from core_api.config import settings
    from core_api.services import memory_service
    from core_api.services.memory_service import create_memory, search_memories

    monkeypatch.setattr(memory_service, "_USE_PIPELINE_SEARCH", use_pipeline)
    # Faithful to production's deferred write rather than hand-inserting a NULL:
    # this is what makes core-api store the row without an embedding.
    monkeypatch.setattr(settings, "deployment_mode", "deferred")

    tid = f"test-tenant-pending-embed-{uuid.uuid4().hex[:8]}"
    token = f"zqxjvbn{uuid.uuid4().hex[:10]}"
    content = (
        "The quarterly irrigation schedule for the northern greenhouse was revised "
        f"to alternate drip cycles on alternate mornings. Reference code {token}."
    )

    created = await create_memory(
        MemoryCreate(
            tenant_id=tid,
            fleet_id=FLEET_ID,
            agent_id=AGENT_ID,
            content=content,
            persist=True,
            entity_links=[],
        )
    )

    # Preconditions. Without these the test could pass vacuously (an embedded
    # row proves nothing about the deferred window) or fail for a fixture
    # reason rather than the behaviour under test: `search_vector` is populated
    # by a trigger created in migration 001, so a metadata-only schema
    # (``Base.metadata.create_all`` with no ``alembic upgrade head``) leaves it
    # NULL and no NULL-embedding row can ever be admitted.
    row = (
        await db.execute(
            text(
                "SELECT embedding IS NULL AS embedding_null, "
                "       search_vector IS NOT NULL AS sv_populated, "
                "       search_vector @@ plainto_tsquery('english', :q) AS fts_matches "
                "FROM memories WHERE id = :mid"
            ),
            {"mid": str(created.id), "q": token},
        )
    ).one()
    assert row.embedding_null, (
        "precondition: deployment_mode=deferred must store the row without an "
        "embedding, otherwise this test says nothing about the deferred window"
    )
    assert row.sv_populated, (
        "precondition: search_vector must be populated — if this fails the schema "
        "was built without migration 001's trigger (run `alembic upgrade head`)"
    )
    assert row.fts_matches, (
        "precondition: the query token must FTS-match the stored content, "
        "otherwise the FTS fallback has nothing to match on"
    )

    results = await search_memories(tenant_id=tid, query=token, top_k=10)

    assert str(created.id) in {str(r.id) for r in results}, (
        f"a memory whose embedding backfill has not landed was not returned by "
        f"recall (caura-memclaw#687). query={token!r} matched the row's content "
        f"exactly and search_vector @@ plainto_tsquery is true, yet search "
        f"returned {len(results)} other row(s): "
        f"{[str(r.id) for r in results]}"
    )


# ---------------------------------------------------------------------------
# Bounds of the FTS-only result reservation (#687)
# ---------------------------------------------------------------------------


class _Row:
    """Minimal stand-in for a scored row: the reservation reads has_embedding only."""

    def __init__(self, rid, has_embedding):
        self.id = rid
        self.has_embedding = has_embedding


def test_fts_only_reservation_is_bounded_and_only_fires_when_needed():
    """The #687 reservation must be a floor of one, not a general reordering.

    It exists so a row that FTS-matches while its embedding is still pending
    stays reachable. It must not become a licence to displace good results, so
    this pins the three boundaries: it promotes at most
    FTS_ONLY_RESERVED_RESULTS, it stays out of the way when the head already
    contains such a row, and it does nothing at all to an ordinary result set.
    """
    from core_api.constants import FTS_ONLY_RESERVED_RESULTS
    from core_api.pipeline.steps.search.post_filter_results import _is_fts_only
    from core_api.search_trim import trim_reserving_fts_only

    def _trim(rows, top_k):
        return trim_reserving_fts_only(rows, top_k, _is_fts_only)

    # No FTS-only rows anywhere → identical to a plain head slice.
    embedded = [_Row(i, True) for i in range(10)]
    assert [r.id for r in _trim(embedded, 5)] == [0, 1, 2, 3, 4]

    # An FTS-only row already inside the head → untouched, nothing promoted.
    head_has_one = [_Row(0, True), _Row(1, False)] + [_Row(i, True) for i in range(2, 10)]
    assert [r.id for r in _trim(head_has_one, 5)] == [0, 1, 2, 3, 4]

    # FTS-only rows only beyond the cutoff → exactly the reserved count is
    # promoted, displacing the same number from the tail of the head, and the
    # result length is unchanged.
    beyond = [_Row(i, True) for i in range(5)] + [_Row(100 + i, False) for i in range(4)]
    out = _trim(beyond, 5)
    assert len(out) == 5, "the reservation must not change how many rows are returned"
    promoted = [r.id for r in out if not r.has_embedding]
    assert len(promoted) == FTS_ONLY_RESERVED_RESULTS, (
        f"promoted {len(promoted)} FTS-only rows, expected exactly "
        f"FTS_ONLY_RESERVED_RESULTS={FTS_ONLY_RESERVED_RESULTS} — an unbounded "
        f"promotion would let a bulk import of unembedded rows flood results"
    )
    # The strongest results survive; only the weakest are displaced.
    assert [r.id for r in out][0] == 0

    # Fewer rows than top_k → head slice semantics preserved.
    assert len(_trim([_Row(0, True)], 5)) == 1

    # The reservation must never consume the ENTIRE head. top_k=1 is a valid
    # input (schemas.py: ge=1), and answering a "give me your single best match"
    # query with only an embed-pending stub — in place of a real, high-scoring
    # result — is a worse answer than not surfacing the stub at all. #687
    # promises such a row is discoverable, not that it outranks the best match;
    # storage's candidate reservation is what keeps that promise here.
    best_then_stub = [_Row(0, True), _Row(100, False)]
    assert [r.id for r in _trim(best_then_stub, 1)] == [0], (
        "a top_k=1 caller must keep their true best match; promoting into the "
        "only slot displaces it entirely rather than displacing the weakest"
    )
    # One slot above the reserved count is the first size that can promote.
    assert [r.id for r in _trim(best_then_stub, FTS_ONLY_RESERVED_RESULTS + 1)] == [0, 100]


def test_fts_only_reservation_constants_stay_in_step_across_services():
    """core-api can only reserve result slots among rows storage sent it.

    ``_FTS_ONLY_RESERVED_CANDIDATES`` (core-storage-api) is the only supply of
    FTS-only candidates the result layer is *guaranteed* — the main branch
    carries such rows just when they earn a slot on score. The two constants sit
    in separately deployed packages and core-storage-api must not import
    core-api, so nothing but this test stops them drifting apart.
    """
    from core_api.constants import FTS_ONLY_RESERVED_RESULTS
    from core_storage_api.services.postgres_service import _FTS_ONLY_RESERVED_CANDIDATES

    assert _FTS_ONLY_RESERVED_CANDIDATES >= FTS_ONLY_RESERVED_RESULTS, (
        f"storage reserves {_FTS_ONLY_RESERVED_CANDIDATES} FTS-only candidate "
        f"slots but core-api tries to reserve {FTS_ONLY_RESERVED_RESULTS} result "
        f"slots — the surplus can only be filled incidentally, so the #687 "
        f"discoverability guarantee silently weakens"
    )
