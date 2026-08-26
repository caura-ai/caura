"""A34 — the retrieval contract for a genuine contradiction: newer value wins.

When a result set contains both a superseded row and its injected successor,
the successor ranks IMMEDIATELY ABOVE its stale predecessor — never appended
unscored at the tail. This is the Hermes/STALE-T2 shape: a query
exact-matching the OLD fact used to return the stale conflicted row at #1
with the current successor dangling last, and the answer LLM picked the
stale value.

Contract carries no new wire fields: the successor's ``supersedes_id`` names
the stale row; the stale row's ``status`` says why it lost.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core_api.pipeline.context import PipelineContext
from core_api.pipeline.steps.search import load_and_serialize as las

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


def _successor_dict(sid, supersedes, content="the corrected fact"):
    return {
        "id": str(sid),
        "tenant_id": "t",
        "fleet_id": None,
        "agent_id": "a1",
        "memory_type": "fact",
        "title": "successor",
        "content": content,
        "weight": 0.5,
        "source_uri": None,
        "run_id": None,
        "metadata_": None,
        "created_at": "2026-08-25T01:00:00Z",
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


async def _run(rows, successors, monkeypatch):
    sc = MagicMock()
    sc.find_successors = AsyncMock(return_value=successors)
    monkeypatch.setattr(las, "get_storage_client", lambda: sc)
    ctx = PipelineContext(data={"filtered_rows": rows, "tenant_id": "t"})
    await las.LoadAndSerialize().execute(ctx)
    return ctx.data["results"]


async def test_successor_ranks_immediately_above_stale_predecessor(monkeypatch):
    """The Hermes shape: stale conflicted row ranked #1 → successor must land
    at #1, stale row demoted to #2, unrelated rows keep relative order."""
    stale_id = uuid.uuid4()
    succ_id = uuid.uuid4()
    other_id = uuid.uuid4()
    rows = [
        _mem_ns(stale_id, status="conflicted", content="the stale fact", score=1.4),
        _mem_ns(other_id, status="active", content="unrelated", score=0.8),
    ]
    out = await _run(rows, [_successor_dict(succ_id, stale_id)], monkeypatch)
    order = [str(m.id) for m in out]
    assert order == [str(succ_id), str(stale_id), str(other_id)]
    # the successor identifies its predecessor; the stale row says why it lost
    assert str(out[0].supersedes_id) == str(stale_id)
    assert out[1].status == "conflicted"
    # injected rows were never scored (D12 contract preserved)
    assert out[0].score is None and out[0].score_parts is None


async def test_stale_row_mid_list_gets_successor_directly_above(monkeypatch):
    a, stale, b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    succ = uuid.uuid4()
    rows = [
        _mem_ns(a, score=2.0),
        _mem_ns(stale, status="outdated", score=1.0),
        _mem_ns(b, score=0.5),
    ]
    out = await _run(rows, [_successor_dict(succ, stale)], monkeypatch)
    assert [str(m.id) for m in out] == [str(a), str(succ), str(stale), str(b)]


async def test_organic_successor_above_keeps_earned_rank(monkeypatch):
    """A successor that already out-ranks its predecessor keeps its earned
    position — no duplication from the injection path either."""
    stale, succ = uuid.uuid4(), uuid.uuid4()
    succ_row = _mem_ns(succ, status="active", content="corrected", score=2.0)
    succ_row.Memory.supersedes_id = stale
    rows = [succ_row, _mem_ns(stale, status="conflicted", score=1.0)]
    out = await _run(rows, [_successor_dict(succ, stale)], monkeypatch)
    assert [str(m.id) for m in out] == [str(succ), str(stale)]
    assert len(out) == 2


async def test_organic_successor_below_is_promoted_above_stale(monkeypatch):
    """The wet-test-discovered case: the correction was recalled on its own
    merit BELOW the stale exact-match (stale un-penalized per A31) — the
    contract promotes it to immediately above. This is the exact live Hermes
    shape: organic recall, no injection involved."""
    stale, succ, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    succ_row = _mem_ns(succ, status="confirmed", content="corrected", score=0.7)
    succ_row.Memory.supersedes_id = stale
    rows = [
        _mem_ns(stale, status="conflicted", content="stale wording", score=0.8),
        succ_row,
        _mem_ns(other, score=0.1),
    ]
    out = await _run(rows, [], monkeypatch)  # no injection — purely organic
    assert [str(m.id) for m in out] == [str(succ), str(stale), str(other)]
    # organic successor keeps its earned score on the wire
    assert out[0].score == 0.7


async def test_supersession_chain_orders_newest_first(monkeypatch):
    """C supersedes B supersedes A -> C, B, A regardless of recall order."""
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    ra = _mem_ns(a, status="outdated", score=2.0)
    rb = _mem_ns(b, status="outdated", score=1.5)
    rb.Memory.supersedes_id = a
    rc = _mem_ns(c, status="active", score=0.2)
    rc.Memory.supersedes_id = b
    out = await _run([ra, rb, rc], [], monkeypatch)
    assert [str(m.id) for m in out] == [str(c), str(b), str(a)]


async def test_multiple_stale_rows_each_get_their_successor(monkeypatch):
    s1, s2 = uuid.uuid4(), uuid.uuid4()
    n1, n2 = uuid.uuid4(), uuid.uuid4()
    rows = [
        _mem_ns(s1, status="outdated", score=2.0),
        _mem_ns(s2, status="conflicted", score=1.0),
    ]
    out = await _run(rows, [_successor_dict(n1, s1), _successor_dict(n2, s2)], monkeypatch)
    assert [str(m.id) for m in out] == [str(n1), str(s1), str(n2), str(s2)]


async def test_no_successors_leaves_order_untouched(monkeypatch):
    a, b = uuid.uuid4(), uuid.uuid4()
    rows = [_mem_ns(a, status="conflicted"), _mem_ns(b)]
    out = await _run(rows, [], monkeypatch)
    assert [str(m.id) for m in out] == [str(a), str(b)]
