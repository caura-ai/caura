"""REST reads answer as their MCP twins do (the REST half of audit batch B26).

Each test names the finding it pins. L-15's recall-event half sits with the
other LogRecallEvent tests in tests/pipeline/test_log_recall_event.py, and
L-124 with the identity tests in tests/test_h06_m30_recall_identity.py.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, Response

from core_api.auth import AuthContext
from core_api.routes import crystallizer, keystones
from core_api.services import recall_service
from core_api.services.recall_service import summarize_memories

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _memory(content: str, ts_valid_start: datetime | None) -> SimpleNamespace:
    mid = str(uuid.uuid4())
    return SimpleNamespace(
        id=mid,
        memory_type="fact",
        title=None,
        status="active",
        content=content,
        ts_valid_start=ts_valid_start,
        model_dump=lambda mode="python": {"id": mid, "content": content},
    )


def _config(recall_enabled: bool) -> SimpleNamespace:
    return SimpleNamespace(
        recall_enabled=recall_enabled,
        recall_provider="fake",
        recall_model="fake-model",
    )


# --- L-15: /recall reports the budget the search ran with ----------------------


@pytest.mark.parametrize("memories", [[], [_memory("a", None)]], ids=["empty", "rows"])
@pytest.mark.parametrize(
    ("diagnostic_ctx", "used"),
    [
        # ExecuteScoredSearch ran: its budget, after the tenant profile (20) and
        # a strategy cap (10), which /search reports as top_k_requested.
        ({"diagnostic_original_top_k": 10, "search_params": {"top_k": 20}}, 10),
        # A short-circuit skipped it: the profile-resolved budget.
        ({"search_params": {"top_k": 20}}, 20),
    ],
    ids=["scored", "short-circuit"],
)
async def test_l15_recall_top_k_used_is_the_resolved_budget(
    memories, diagnostic_ctx, used
):
    # The caller named no top_k, so the request carries the schema default.
    out = await summarize_memories(
        memories,
        "q",
        _config(recall_enabled=False),
        diagnostic=True,
        diagnostic_ctx=diagnostic_ctx,
        top_k=5,
    )
    assert out["diagnostic"]["top_k_used"] == used


# --- L-136: /recall keeps the rank order ----------------------------------------


def _ranked() -> list[SimpleNamespace]:
    """Rank order that is not date order: the best hit is the newest."""
    return [
        _memory("newest, ranked first", datetime(2026, 3, 1, tzinfo=UTC)),
        _memory("undated, ranked second", None),
        _memory("oldest, ranked third", datetime(2026, 1, 1, tzinfo=UTC)),
    ]


async def test_l136_recall_rows_keep_the_rank_order():
    memories = _ranked()
    ranked_ids = [m.id for m in memories]

    out = await summarize_memories(memories, "q", _config(recall_enabled=False))

    assert [row["id"] for row in out["memories"]] == ranked_ids
    assert [m.id for m in memories] == ranked_ids, "the caller's list was reordered"


async def test_l136_the_brief_reads_a_timeline_and_returns_the_ranking(monkeypatch):
    monkeypatch.setattr(
        recall_service, "call_with_fallback", AsyncMock(return_value="brief")
    )
    memories = _ranked()
    ranked_ids = [m.id for m in memories]

    out = await summarize_memories(
        memories, "q", _config(recall_enabled=True), diagnostic=True
    )

    prompt = out["diagnostic"]["recall_prompt"]
    chronological = (
        "undated, ranked second",
        "oldest, ranked third",
        "newest, ranked first",
    )
    positions = [prompt.index(text) for text in chronological]
    assert positions == sorted(positions), "the LLM should still read a timeline"
    assert [row["id"] for row in out["memories"]] == ranked_ids


# --- L-120: a cross-tenant read key lists the reports it may read -----------------


def _reader() -> AuthContext:
    """A cross-tenant read key: home tenant-a, may also read tenant-b."""
    return AuthContext(
        tenant_id="tenant-a", readable_tenant_ids=["tenant-a", "tenant-b"]
    )


def _report_storage(monkeypatch) -> MagicMock:
    sc = MagicMock(name="storage_client")
    sc.list_reports = AsyncMock(return_value=[])
    sc.get_latest_report = AsyncMock(return_value=None)
    monkeypatch.setattr(crystallizer, "get_storage_client", lambda: sc)
    return sc


async def test_l120_list_reports_reads_a_readable_tenant(monkeypatch):
    sc = _report_storage(monkeypatch)

    await crystallizer.list_reports(
        tenant_id="tenant-b", limit=10, offset=0, auth=_reader()
    )

    assert sc.list_reports.await_args.args[0] == "tenant-b"


async def test_l120_latest_report_reads_a_readable_tenant(monkeypatch):
    sc = _report_storage(monkeypatch)

    await crystallizer.get_latest_report(tenant_id="tenant-b", auth=_reader())

    assert sc.get_latest_report.await_args.args[0] == "tenant-b"


@pytest.mark.parametrize("route", ["list", "latest"])
async def test_l120_an_unreadable_tenant_is_still_refused(monkeypatch, route):
    sc = _report_storage(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        if route == "list":
            await crystallizer.list_reports(
                tenant_id="tenant-c", limit=10, offset=0, auth=_reader()
            )
        else:
            await crystallizer.get_latest_report(tenant_id="tenant-c", auth=_reader())

    assert exc.value.status_code == 403
    assert sc.list_reports.await_count == sc.get_latest_report.await_count == 0


# --- L-122: an agent with no fleet_id reads its home fleet's rules ---------------


async def _list_keystones(monkeypatch, *, fleet_id, agent_id, agent_row):
    sc = MagicMock(name="storage_client")
    sc.list_keystones = AsyncMock(return_value=([], False))
    monkeypatch.setattr(keystones, "get_storage_client", lambda: sc)
    lookup = AsyncMock(return_value=agent_row)
    # ``raising=False``: before the fix the route had no lookup to stub, and
    # the assertion on what reached storage is the one that should fail.
    monkeypatch.setattr(keystones, "lookup_agent", lookup, raising=False)
    await keystones.list_keystones(
        Response(),
        tenant_id="tenant-a",
        fleet_id=fleet_id,
        agent_id=agent_id,
        envelope=False,
        auth=AuthContext(tenant_id="tenant-a"),
    )
    return sc.list_keystones.await_args.kwargs, lookup


async def test_l122_an_agent_without_a_fleet_reads_its_home_fleet(monkeypatch):
    sent, _ = await _list_keystones(
        monkeypatch,
        fleet_id=None,
        agent_id="alice",
        agent_row={"agent_id": "alice", "fleet_id": "fleet-a"},
    )

    assert (sent["fleet_id"], sent["agent_id"]) == ("fleet-a", "alice")


async def test_l122_an_unregistered_agent_reads_the_tenant_rules(monkeypatch):
    # As on MCP: no row means no home fleet, so the agent filter has nothing to
    # resolve against and the tenant's rules come back.
    sent, _ = await _list_keystones(
        monkeypatch, fleet_id=None, agent_id="ghost", agent_row=None
    )

    assert (sent["fleet_id"], sent["agent_id"]) == (None, None)


async def test_l122_a_named_fleet_needs_no_lookup(monkeypatch):
    sent, lookup = await _list_keystones(
        monkeypatch,
        fleet_id="fleet-b",
        agent_id="alice",
        agent_row={"agent_id": "alice", "fleet_id": "fleet-a"},
    )

    assert (sent["fleet_id"], sent["agent_id"]) == ("fleet-b", "alice")
    assert lookup.await_count == 0
