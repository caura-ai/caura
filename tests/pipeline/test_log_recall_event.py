"""Unit tests for LogRecallEvent — gating + candidate construction (no DB).

The step must:
  - skip unless source == "mcp_recall" (plugin /search is never logged),
  - skip unless the tenant opted in (recall_logging_enabled),
  - otherwise emit one event + returned candidates + capped near-misses.
The actual DB write (`_persist`) and `track_task` are patched out.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core_api.pipeline.context import PipelineContext
from core_api.pipeline.steps.search import log_recall_event as mod
from core_api.pipeline.steps.search.log_recall_event import LogRecallEvent


def _row(vec_sim, score, recall_boost=1.0):
    return SimpleNamespace(
        Memory=SimpleNamespace(id=uuid.uuid4()),
        vec_sim=vec_sim,
        score=score,
        recall_boost=recall_boost,
    )


def _cfg(enabled):
    return SimpleNamespace(recall_logging_enabled=enabled)


def _capture(monkeypatch):
    """Patch _persist + track_task; return a list that receives (event, candidates)."""
    captured = []

    def fake_persist(event, candidates):
        captured.append((event, candidates))
        return "coro-sentinel"

    monkeypatch.setattr(mod, "_persist", fake_persist)
    monkeypatch.setattr(mod, "track_task", lambda coro: None)
    return captured


def _ctx(source, enabled, returned_rows, raw_rows):
    return PipelineContext(
                data={
            "source": source,
            "query": "what is our brand rule",
            "tenant_id": "t1",
            "caller_agent_id": "brandclaw",
            "filter_agent_id": None,
            "fleet_ids": ["yoniclaw-fleet"],
            "top_k": 5,
            "search_params": {"min_similarity": 0.3},
            "retrieval_plan": None,
            "filtered_rows": returned_rows,
            "raw_rows": raw_rows,
        },
        tenant_config=_cfg(enabled),
    )


@pytest.mark.asyncio
async def test_skips_when_source_is_not_mcp_recall(monkeypatch):
    captured = _capture(monkeypatch)
    ctx = _ctx("search", True, [_row(0.6, 0.55)], [_row(0.6, 0.55)])
    await LogRecallEvent().execute(ctx)
    assert captured == []  # plugin /search must never be logged


@pytest.mark.asyncio
async def test_skips_when_tenant_not_opted_in(monkeypatch):
    captured = _capture(monkeypatch)
    ctx = _ctx("mcp_recall", False, [_row(0.6, 0.55)], [_row(0.6, 0.55)])
    await LogRecallEvent().execute(ctx)
    assert captured == []


@pytest.mark.asyncio
async def test_logs_event_with_returned_and_near_misses(monkeypatch):
    captured = _capture(monkeypatch)
    returned = [_row(0.60, 0.58), _row(0.55, 0.54)]
    # raw_rows = the two returned + 7 below-floor near-misses
    near = [_row(0.28 - i * 0.01, 0.2) for i in range(7)]
    raw = returned + near
    ctx = _ctx("mcp_recall", True, returned, raw)

    await LogRecallEvent().execute(ctx)

    assert len(captured) == 1
    event, candidates = captured[0]
    assert event["source"] == "mcp_recall"
    assert event["query_text"] == "what is our brand rule"
    assert event["fleet_scope"] == "yoniclaw-fleet"
    assert event["result_count"] == 2
    assert event["min_similarity"] == 0.3

    # 2 returned + capped 5 near-misses = 7 candidate rows
    assert len(candidates) == 7
    returned_flags = [c["returned"] for c in candidates]
    assert returned_flags == [True, True, False, False, False, False, False]
    assert [c["rank"] for c in candidates] == [1, 2, 3, 4, 5, 6, 7]
    # raw cosine + final score are captured (this is the signal we were missing)
    assert candidates[0]["vec_sim"] == 0.60
    assert candidates[0]["final_score"] == 0.58
    # memory_id is stringified — the payload now crosses an HTTP/JSON boundary,
    # so every candidate id must be a str (UUIDs aren't JSON-serializable).
    assert all(isinstance(c["memory_id"], str) for c in candidates)


@pytest.mark.asyncio
async def test_persist_routes_to_storage_client():
    """``_persist`` posts the event + candidates through the storage client
    (no direct DB session) and swallows failures."""
    from unittest.mock import AsyncMock, MagicMock, patch

    event = {"tenant_id": "t1", "source": "mcp_recall"}
    candidates = [{"rank": 1, "memory_id": str(uuid.uuid4()), "returned": True}]

    sc = MagicMock()
    sc.log_recall = AsyncMock(return_value="evt-1")
    with patch.object(mod, "get_storage_client", return_value=sc):
        await mod._persist(event, candidates)

    sc.log_recall.assert_awaited_once_with(event, candidates)
