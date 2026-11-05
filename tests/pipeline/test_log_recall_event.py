"""Unit tests for LogRecallEvent — gating + candidate construction (no DB).

Two independent, per-tenant modes (on by default, an explicit False opts out):
  - source == "mcp_recall" + recall_logging_enabled
      → FULL log: returned candidates + capped below-floor near-misses.
  - source == "search" + search_recall_logging_enabled
      → LIGHT log: returned candidates only (no near-misses).
  - any other source, or the relevant flag off → not logged.
The actual write (`_persist`, which posts to the storage client) and
`track_task` are patched out.
"""

import uuid
from types import SimpleNamespace

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


def _cfg(recall=False, search=False, near_miss_rate=0.0):
    return SimpleNamespace(
        recall_logging_enabled=recall,
        search_recall_logging_enabled=search,
        search_recall_near_miss_sample_rate=near_miss_rate,
    )


def _capture(monkeypatch):
    """Patch _persist + track_task; return a list that receives (event, candidates)."""
    captured = []

    def fake_persist(event, candidates):
        captured.append((event, candidates))
        return "coro-sentinel"

    monkeypatch.setattr(mod, "_persist", fake_persist)
    monkeypatch.setattr(mod, "track_task", lambda _coro: None)
    return captured


def _ctx(source, cfg, returned_rows, raw_rows):
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
        tenant_config=cfg,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("profile_top_k", "final_top_k", "logged"),
    [
        # The tenant's default profile set 10; the request carried the default 5.
        (10, 10, 10),
        # RECENT_CONTEXT capped the profile's 20 to 5: the cap is what ran.
        (20, 5, 5),
        # A short-circuit skipped ExecuteScoredSearch: the profile's budget.
        (10, None, 10),
    ],
    ids=["profile", "strategy-cap", "short-circuit"],
)
async def test_l15_the_event_logs_the_budget_the_search_ran_with(
    monkeypatch, profile_top_k, final_top_k, logged
):
    captured = _capture(monkeypatch)
    ctx = _ctx("mcp_recall", _cfg(recall=True), [_row(0.6, 0.55)], [_row(0.6, 0.55)])
    ctx.data["search_params"]["top_k"] = profile_top_k
    if final_top_k is not None:
        ctx.data["final_top_k"] = final_top_k

    await LogRecallEvent().execute(ctx)

    [(event, _)] = captured
    assert event["top_k"] == logged


@pytest.mark.asyncio
async def test_skips_search_when_search_flag_off(monkeypatch):
    captured = _capture(monkeypatch)
    # recall_logging_enabled on, but the search flag is off → /search not logged.
    ctx = _ctx(
        "search", _cfg(recall=True, search=False), [_row(0.6, 0.55)], [_row(0.6, 0.55)]
    )
    await LogRecallEvent().execute(ctx)
    assert captured == []


@pytest.mark.asyncio
async def test_skips_mcp_when_recall_flag_off(monkeypatch):
    captured = _capture(monkeypatch)
    ctx = _ctx(
        "mcp_recall",
        _cfg(recall=False, search=True),
        [_row(0.6, 0.55)],
        [_row(0.6, 0.55)],
    )
    await LogRecallEvent().execute(ctx)
    assert captured == []


@pytest.mark.asyncio
async def test_skips_unknown_source(monkeypatch):
    captured = _capture(monkeypatch)
    ctx = _ctx(
        "bulk", _cfg(recall=True, search=True), [_row(0.6, 0.55)], [_row(0.6, 0.55)]
    )
    await LogRecallEvent().execute(ctx)
    assert captured == []


@pytest.mark.asyncio
async def test_mcp_logs_returned_and_near_misses(monkeypatch):
    captured = _capture(monkeypatch)
    returned = [_row(0.60, 0.58), _row(0.55, 0.54)]
    # raw_rows = the two returned + 7 below-floor near-misses
    near = [_row(0.28 - i * 0.01, 0.2) for i in range(7)]
    raw = returned + near
    ctx = _ctx("mcp_recall", _cfg(recall=True), returned, raw)

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
async def test_search_logs_returned_only_no_near_misses(monkeypatch):
    captured = _capture(monkeypatch)
    returned = [_row(0.60, 0.58), _row(0.55, 0.54)]
    near = [_row(0.28 - i * 0.01, 0.2) for i in range(7)]
    raw = returned + near
    ctx = _ctx("search", _cfg(search=True), returned, raw)

    await LogRecallEvent().execute(ctx)

    assert len(captured) == 1
    event, candidates = captured[0]
    assert event["source"] == "search"
    assert event["result_count"] == 2
    # LIGHT mode: only the 2 returned rows, NO near-misses.
    assert len(candidates) == 2
    assert [c["returned"] for c in candidates] == [True, True]
    assert [c["rank"] for c in candidates] == [1, 2]
    # scores still captured for the returned rows.
    assert candidates[0]["vec_sim"] == 0.60
    assert candidates[0]["final_score"] == 0.58
    assert all(isinstance(c["memory_id"], str) for c in candidates)


@pytest.mark.asyncio
async def test_search_keeps_near_misses_when_sampled(monkeypatch):
    # near_miss_rate=1.0 → always sampled (random.random() < 1.0 is always True),
    # so the /search path keeps near-misses on this event.
    captured = _capture(monkeypatch)
    returned = [_row(0.60, 0.58), _row(0.55, 0.54)]
    near = [_row(0.28 - i * 0.01, 0.2) for i in range(7)]
    raw = returned + near
    ctx = _ctx("search", _cfg(search=True, near_miss_rate=1.0), returned, raw)

    await LogRecallEvent().execute(ctx)

    assert len(captured) == 1
    event, candidates = captured[0]
    assert event["source"] == "search"
    # 2 returned + capped 5 near-misses = 7 (same cap as mcp_recall).
    assert len(candidates) == 7
    assert [c["returned"] for c in candidates] == [
        True,
        True,
        False,
        False,
        False,
        False,
        False,
    ]


@pytest.mark.asyncio
async def test_the_logged_query_has_pii_masked(monkeypatch):
    """The log keeps the query's text, so it keeps it masked; the search
    itself still sees the query as asked."""
    captured = _capture(monkeypatch)
    returned = [_row(0.60, 0.58)]
    ctx = _ctx("mcp_recall", _cfg(recall=True), returned, returned)
    query = "when is dana.levi@example.com back from leave"
    ctx.data["query"] = query

    await LogRecallEvent().execute(ctx)

    logged = captured[0][0]["query_text"]
    assert "dana.levi@example.com" not in logged
    assert logged.startswith("when is ")
    assert logged.endswith(" back from leave")
    assert ctx.data["query"] == query


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


@pytest.mark.parametrize(
    ("observability", "expected"),
    [
        # A tenant with no settings at all logs both paths (the default).
        ({}, (True, True)),
        # An explicit False is an opt-out the default never overrides.
        ({"recall_logging_enabled": False}, (False, True)),
        ({"search_recall_logging_enabled": False}, (True, False)),
        (
            {"recall_logging_enabled": False, "search_recall_logging_enabled": False},
            (False, False),
        ),
    ],
    ids=["unset", "recall-off", "search-off", "both-off"],
)
def test_recall_logging_is_on_by_default_and_opt_out(observability, expected):
    from core_api.services.organization_settings import ResolvedConfig

    cfg = ResolvedConfig({"observability": observability} if observability else None)
    assert (cfg.recall_logging_enabled, cfg.search_recall_logging_enabled) == expected


@pytest.mark.asyncio
async def test_a_fresh_tenant_logs_both_paths(monkeypatch):
    """A tenant with no settings row: both ``mcp_recall`` and ``search`` log."""
    from core_api.services.organization_settings import ResolvedConfig

    captured = _capture(monkeypatch)
    cfg = ResolvedConfig()
    for source in ("mcp_recall", "search"):
        await LogRecallEvent().execute(
            _ctx(source, cfg, [_row(0.6, 0.55)], [_row(0.6, 0.55)])
        )

    assert [ev["source"] for ev, _ in captured] == ["mcp_recall", "search"]
