"""D16 — successor injection is budgeted and labelled.

Before this row, injection was additive with no bound and no marker: every
outdated/conflicted row in the already-trimmed result set pulled in EVERY row
that superseded it (``supersedes_id IN (...)`` has no per-predecessor cap), so
a top_k=5 call answered with 10 items — 53 on a conflict-heavy store — and
nothing on the wire said which rows the query had actually recalled.

The contract now:
  * at most ONE injected successor per stale row — the newest — so a response
    holds at most 2*top_k items;
  * every injected row is marked ``injected: true`` (beside its ``score:
    null``); a successor recalled on its own merit stays unmarked;
  * the response is deliberately NOT re-trimmed to top_k — evicting a
    successor or its predecessor at the boundary would reintroduce the
    stale-answer failure A34 exists to prevent;
  * ``top_k``'s own description states all of this instead of promising a
    maximum the behavior never kept.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core_api.pipeline.context import PipelineContext
from core_api.pipeline.steps.search import load_and_serialize as las
from core_api.schemas import MemoryOut, SearchRequest

pytestmark = pytest.mark.unit


def _mem_ns(mid, status="active", content="c", **score):
    return SimpleNamespace(
        Memory=SimpleNamespace(
            id=mid,
            tenant_id="t",
            fleet_id=None,
            agent_id="a1",
            agent_display_name=None,
            memory_type="fact",
            title="row",
            content=content,
            weight=0.5,
            source_uri=None,
            run_id=None,
            metadata_=None,
            created_at="2026-08-25T00:00:00Z",
            expires_at=None,
            subject_entity_id=None,
            predicate=None,
            object_value=None,
            ts_valid_start=None,
            ts_valid_end=None,
            status=status,
            visibility="scope_fleet",
            recall_count=0,
            last_recalled_at=None,
            supersedes_id=None,
        ),
        score=score.get("score", 1.0),
        similarity=None,
        vec_sim=score.get("vec_sim", 0.9),
        fts_score=None,
        freshness=None,
        entity_boost=None,
        recall_boost=None,
        temporal_boost=None,
        status_penalty=None,
        has_embedding=True,
        entity_links=[],
    )


def _successor_dict(sid, supersedes, created_at="2026-08-25T01:00:00Z"):
    return {
        "id": str(sid),
        "tenant_id": "t",
        "fleet_id": None,
        "agent_id": "a1",
        "memory_type": "fact",
        "title": "successor",
        "content": "the corrected fact",
        "weight": 0.5,
        "source_uri": None,
        "run_id": None,
        "metadata_": None,
        "created_at": created_at,
        "expires_at": None,
        "subject_entity_id": None,
        "predicate": None,
        "object_value": None,
        "ts_valid_start": None,
        "ts_valid_end": None,
        "status": "active",
        "visibility": "scope_fleet",
        "recall_count": 0,
        "last_recalled_at": None,
        "supersedes_id": str(supersedes),
    }


async def _run(rows, successors, monkeypatch) -> PipelineContext:
    sc = MagicMock()
    sc.find_successors = AsyncMock(return_value=successors)
    monkeypatch.setattr(las, "get_storage_client", lambda: sc)
    ctx = PipelineContext(data={"filtered_rows": rows, "tenant_id": "t"})
    await las.LoadAndSerialize().execute(ctx)
    return ctx


# ── budget ────────────────────────────────────────────────────────────────


async def test_one_injected_successor_per_stale_row_newest_wins(monkeypatch):
    """Three rows supersede the same stale row; only the newest is injected,
    regardless of the order storage returned them in."""
    stale = uuid.uuid4()
    old, older, newest = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    ctx = await _run(
        [_mem_ns(stale, status="conflicted", score=1.0)],
        [
            _successor_dict(old, stale, created_at="2026-08-25T02:00:00Z"),
            _successor_dict(newest, stale, created_at="2026-08-25T03:00:00Z"),
            _successor_dict(older, stale, created_at="2026-08-25T01:00:00Z"),
        ],
        monkeypatch,
    )
    out = ctx.data["results"]
    assert [str(m.id) for m in out] == [str(newest), str(stale)]


async def test_response_holds_at_most_twice_top_k(monkeypatch):
    """The register's live shape: every trimmed row stale, heavy successor
    fan-out. top_k=5 used to answer with up to 53 items; the budget bounds it
    at 10 while keeping the A34 pairing for every stale row."""
    top_k = 5
    stale_ids = [uuid.uuid4() for _ in range(top_k)]
    rows = [_mem_ns(sid, status="outdated", score=1.0) for sid in stale_ids]
    successors = []
    for sid in stale_ids:
        for hour in ("01", "02", "03"):
            successors.append(
                _successor_dict(uuid.uuid4(), sid, created_at=f"2026-08-25T{hour}:00:00Z")
            )
    ctx = await _run(rows, successors, monkeypatch)
    out = ctx.data["results"]
    assert len(out) == 2 * top_k
    # A34 pairing survives the budget: each stale row sits directly under the
    # one successor injected for it.
    for i, m in enumerate(out):
        if m.status == "outdated":
            assert str(out[i - 1].supersedes_id) == str(m.id)
            assert out[i - 1].injected is True


async def test_successful_injection_stays_out_of_warnings(monkeypatch):
    """A28's ``warnings`` mean the enrichment is INCOMPLETE. The budget marks
    rows instead of warning about them — a normal injection must not start
    emitting warnings, or the incomplete signal stops meaning anything."""
    stale = uuid.uuid4()
    ctx = await _run(
        [_mem_ns(stale, status="conflicted")],
        [_successor_dict(uuid.uuid4(), stale)],
        monkeypatch,
    )
    assert ctx.data.get("warnings") is None


# ── label ─────────────────────────────────────────────────────────────────


async def test_injected_row_is_marked_and_recalled_rows_are_not(monkeypatch):
    stale, other = uuid.uuid4(), uuid.uuid4()
    succ = uuid.uuid4()
    ctx = await _run(
        [_mem_ns(stale, status="conflicted", score=1.0), _mem_ns(other, score=0.5)],
        [_successor_dict(succ, stale)],
        monkeypatch,
    )
    by_id = {str(m.id): m for m in ctx.data["results"]}
    assert by_id[str(succ)].injected is True
    # the register's shape: injected: true beside score: null
    assert by_id[str(succ)].score is None
    assert by_id[str(stale)].injected is False
    assert by_id[str(other)].injected is False


async def test_organic_successor_is_not_marked_injected(monkeypatch):
    """A successor the query recalled on its own merit is a result, not an
    injection — and it satisfies the budget, so no sibling is injected for
    its predecessor either."""
    stale, succ = uuid.uuid4(), uuid.uuid4()
    organic = _mem_ns(succ, status="active", content="corrected", score=2.0)
    organic.Memory.supersedes_id = stale
    ctx = await _run(
        [organic, _mem_ns(stale, status="conflicted", score=1.0)],
        [_successor_dict(succ, stale)],
        monkeypatch,
    )
    out = ctx.data["results"]
    assert [str(m.id) for m in out] == [str(succ), str(stale)]
    assert out[0].injected is False and out[0].score == 2.0


async def test_newer_correction_is_injected_above_an_older_organic_one(monkeypatch):
    """An organic successor does not exhaust the predecessor's budget when a
    NEWER correction exists: the newest is injected (marked), the organic row
    keeps its earned place and stays unmarked."""
    stale, organic_id, newest = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    organic = _mem_ns(organic_id, status="active", content="older correction", score=0.9)
    organic.Memory.supersedes_id = stale
    organic.Memory.created_at = "2026-08-25T01:00:00Z"
    ctx = await _run(
        [organic, _mem_ns(stale, status="conflicted", score=1.0)],
        [
            _successor_dict(organic_id, stale, created_at="2026-08-25T01:00:00Z"),
            _successor_dict(newest, stale, created_at="2026-08-25T02:00:00Z"),
        ],
        monkeypatch,
    )
    out = ctx.data["results"]
    assert [str(m.id) for m in out] == [str(organic_id), str(newest), str(stale)]
    assert [m.injected for m in out] == [False, True, False]


# ── wire contract ─────────────────────────────────────────────────────────


def test_injected_defaults_false_everywhere_else():
    """Create/get/list build MemoryOut without the flag; the default must keep
    them truthful rather than requiring every call site to say ``False``."""
    assert MemoryOut.model_fields["injected"].default is False


def test_top_k_description_states_the_injection_contract():
    """D16's third clause: the tool surface documents that top_k bounds the
    recall, not the response, and names the marker a caller should look for."""
    desc = SearchRequest.model_fields["top_k"].description or ""
    assert "injected" in desc
    assert "2*top_k" in desc
