"""ax-0917-h-01 / h-02 — the surfaces #1707 left without attribution.

#1707 armed ``core_api.request_phase`` from ``RequestTimeoutMiddleware``, so a
504 on ``/search`` now names the layer that ate the budget. Its own handover
recorded what it did NOT cover, and that is what these tests are for:

* three routes opt OUT of that middleware (``_TIMEOUT_OPT_OUT_PATHS``) and
  enforce their own ``asyncio.wait_for`` instead — ``/memories/bulk`` and
  ``/interview/submit`` replace the deadline but nothing armed the recorder,
  so their 504s were the pre-#1707 failure verbatim;
* the MCP mount is skipped entirely (``is_mcp_path``), and MCP is the surface
  agents actually use.

Same method as ``test_ax_h01_h02_timeout_attribution``: the real route, the
real pipeline, the real middleware order, one hop stalled at a time, with only
the deadline lowered. No provider calls and no LLM calls — every stalled hop is
patched.

Note the deadlines here are reachable in a way the middleware's is not. The
middleware captures its budget at ``add_middleware`` time (hence that file's
walk of ``app.middleware_stack``); both route budgets are read from
``app_settings`` at call time, so monkeypatching the settings object is the
production path, not a way around it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import pytest

from tests.conftest import get_test_auth, new_tenant_id, uid

pytestmark = pytest.mark.asyncio

# Far past the budget: a stall that could finish would make the test a race.
_STALL_S = 30.0


# Released in teardown so a stalled hop does not outlive its test — the same
# hazard ``test_ax_h01_h02_timeout_attribution`` documents, for the same
# reason (the storage client shields in-flight requests on purpose).
_release: asyncio.Event | None = None


@pytest.fixture(autouse=True)
def stall_gate():
    global _release
    _release = asyncio.Event()
    yield
    _release.set()
    _release = None


async def _stall_forever(*_a, **_kw):
    gate = _release
    if gate is None:
        await asyncio.sleep(_STALL_S)
    else:
        await asyncio.wait_for(gate.wait(), _STALL_S)
    return []


def _budget_log(caplog, message: str) -> logging.LogRecord:
    hits = [r for r in caplog.records if r.getMessage().startswith(message)]
    assert hits, f"no {message!r} log; saw {[r.getMessage() for r in caplog.records]}"
    return hits[-1]


# ===========================================================================
# 1 — /memories/bulk, the opt-out route a customer bulk-ingest runs
# ===========================================================================


async def _timed_out_bulk(client, prefix: str) -> tuple[int, dict]:
    tenant_id, headers = get_test_auth()
    resp = await client.post(
        "/api/v1/memories/bulk",
        json={
            "tenant_id": tenant_id,
            "agent_id": f"{prefix}-{uid()}",
            "items": [{"content": f"{prefix}-content-{uid()}"}],
        },
        headers={**headers, "X-Bulk-Attempt-Id": f"{prefix}-{uid()}"},
    )
    return resp.status_code, resp.json()


def _stall_bulk_storage(monkeypatch):
    """Hang the storage roundtrip AS DEEP AS IT GOES.

    Patched at ``postgres_service.memory_add_all`` — past the storage client,
    past its retry policy, past the storage app's own router — so the request
    really does traverse every layer the phase labels name. Patching the
    client method instead would replace the very frame that records the
    ``storage.*`` phase, and the test would then pass against a stack that
    never recorded anything.
    """
    from core_storage_api.services.postgres_service import PostgresService

    monkeypatch.setattr(PostgresService, "memory_add_all", _stall_forever)


def _stall_bulk_storage_slot(monkeypatch, tenant_id: str):
    """Saturate the storage-write bulkhead so the QUEUE is what blocks.

    ``per_tenant_storage_slot`` queues unboundedly and names the request
    budget as its only cap — on this route that budget is the one below, and
    a queue with no free slots looks exactly like a slow backend end to end
    while being fixed by capacity rather than by the backend.
    """
    import core_api.middleware.per_tenant_concurrency as ptc

    ptc._TENANT_SEMAPHORES[("storage_write", tenant_id)] = asyncio.Semaphore(0)


async def test_the_bulk_504_names_the_layer_that_ate_its_budget(
    client, monkeypatch, caplog
):
    """The question h-01 asked of ``/search``, asked of the route most able to
    need it: 90s of budget across embed, enrich, an unbounded slot acquire and
    the storage roundtrip, on the path a customer's bulk ingest runs.

    Before this change the 504 carried a bare sentence and the status-derived
    ``UPSTREAM_TIMEOUT`` — no phase, no budget, no elapsed, and a code
    claiming a backend had reported a failure when none had.
    """
    from core_api import config as cfg

    monkeypatch.setattr(cfg.settings, "bulk_request_timeout_seconds", 0.4)
    monkeypatch.setattr(cfg.settings, "storage_bulk_timeout_seconds", 30.0)
    _stall_bulk_storage(monkeypatch)

    with caplog.at_level(logging.WARNING):
        status, body = await _timed_out_bulk(client, "bulkphase")

    assert status == 504, body
    err = body["error"]
    assert err["code"] == "REQUEST_BUDGET_EXCEEDED", (
        "a 504 from our own deadline must not claim an upstream timed out"
    )
    assert err["details"]["phase"] == "storage.POST /memories/bulk"
    assert err["details"]["budget_seconds"] == 0.4
    assert err["details"]["elapsed_seconds"] >= 0.4
    assert err["details"]["path"] == "/api/v1/memories/bulk"
    assert "storage.POST /memories/bulk" in err["message"]


async def test_the_bulk_504_still_carries_the_retry_contract(client, monkeypatch):
    """The attribution rides ALONGSIDE the recovery contract, not instead of
    it. ``detail`` stays the plain sentence naming the header a retry must
    resend — the property ``test_bulk_atomicity`` pins and a deployed client
    already reads."""
    from core_api import config as cfg

    monkeypatch.setattr(cfg.settings, "bulk_request_timeout_seconds", 0.4)
    monkeypatch.setattr(cfg.settings, "storage_bulk_timeout_seconds", 30.0)
    _stall_bulk_storage(monkeypatch)

    status, body = await _timed_out_bulk(client, "bulkretry")
    assert status == 504
    assert isinstance(body["detail"], str)
    assert "X-Bulk-Attempt-Id" in body["detail"]


async def test_a_saturated_bulk_bulkhead_is_not_reported_as_a_slow_backend(
    client, monkeypatch
):
    """Same discrimination h-02 needed on ``/search``, on the write side: a
    queued slot acquire and a stalled storage call have opposite fixes, and
    the acquire's only prior signal was a DEBUG line prod never emits."""
    import core_api.middleware.per_tenant_concurrency as ptc
    from core_api import config as cfg

    monkeypatch.setattr(cfg.settings, "bulk_request_timeout_seconds", 0.4)
    monkeypatch.setattr(cfg.settings, "storage_bulk_timeout_seconds", 30.0)
    tenant_id, _ = get_test_auth()
    _stall_bulk_storage_slot(monkeypatch, tenant_id)
    try:
        status, body = await _timed_out_bulk(client, "bulkslot")
    finally:
        ptc._TENANT_SEMAPHORES.pop(("storage_write", tenant_id), None)

    assert status == 504, body
    details = body["error"]["details"]
    assert details["phase"] == "slot_acquire.storage_write"
    assert "storage.POST /memories/bulk" not in [
        p["phase"] for p in details["phases_cancelled"]
    ], "a request still queued for a slot never reached the storage call"


async def test_the_bulk_timeout_log_carries_the_phase_for_aggregation(
    client, monkeypatch, caplog
):
    """Body attribution helps the one caller who got the 504. "Which layer is
    eating bulk budgets, how often" is a log question, so the phase has to be
    a flat top-level field — and the same field name the middleware uses, or
    the two paths cannot be grouped together."""
    from core_api import config as cfg

    monkeypatch.setattr(cfg.settings, "bulk_request_timeout_seconds", 0.4)
    monkeypatch.setattr(cfg.settings, "storage_bulk_timeout_seconds", 30.0)
    _stall_bulk_storage(monkeypatch)

    with caplog.at_level(logging.WARNING):
        await _timed_out_bulk(client, "bulklog")

    rec = _budget_log(caplog, "bulk write timed out")
    assert rec.phase == "storage.POST /memories/bulk"
    assert rec.path == "/api/v1/memories/bulk"
    assert rec.budget_seconds == 0.4
    assert rec.elapsed_seconds >= 0.4


# ===========================================================================
# 2 — a swallowed early failure must not hijack the attribution
# ===========================================================================


async def test_a_hop_that_failed_early_is_not_named_as_the_one_that_timed_out():
    """The recorder files a failed phase by its position in the unwind, and
    on a cancellation that ordering IS the stack. It is not, when a hop failed
    and the caller swallowed it and carried on — bulk embed does exactly that
    on a deferred deployment. Filed as a cancellation, the swallowed hop sorts
    ahead of the real culprit (it unwound first) and ``phase`` names something
    that finished long before the deadline: a confidently wrong attribution,
    which is worse than the none this module replaced.
    """
    from core_api import request_phase

    with request_phase.own_deadline(60.0) as phases:
        # A hop that fails early and is swallowed.
        try:
            with request_phase.phase("embed.bulk"):
                raise RuntimeError("provider refused; falling through to backfill")
        except RuntimeError:
            pass
        # ... then the request runs on and the budget blows somewhere else.
        phases._deadline = phases._started_at  # the deadline is now in the past
        try:
            with request_phase.phase("storage.POST /memories/bulk"):
                raise asyncio.CancelledError
        except asyncio.CancelledError:
            pass

        snap = phases.snapshot()

    assert snap["phase"] == "storage.POST /memories/bulk"
    assert [p["phase"] for p in snap["phases_cancelled"]] == [
        "storage.POST /memories/bulk"
    ]
    assert [p["phase"] for p in snap["phases_failed"]] == ["embed.bulk"], (
        "the early failure is still reported — it is often WHY the request "
        "ran long — just not as the layer holding the budget at the deadline"
    )


# ===========================================================================
# 3 — /interview/submit, the other opt-out route with its own deadline
# ===========================================================================


async def test_the_interview_504_names_which_half_of_the_window_stalled(
    client, monkeypatch, caplog
):
    """The interview budget is spent on two halves with different owners: a
    chain of map-phase LLM calls, then the bulk write it feeds. A 504 that
    names neither sends an operator to guess between a provider and storage.

    The stall is INSIDE ``_interview_chunk``, at the provider fallback chain
    it delegates to — so no provider is contacted and no LLM call is made,
    while the frame that records the phase is the real one. Patching
    ``_interview_chunk`` itself would replace that frame, and the test would
    pass against a stack that recorded nothing.
    """
    from core_api import config as cfg
    from core_api.providers import _retry

    monkeypatch.setattr(cfg.settings, "interview_request_timeout_seconds", 0.4)
    monkeypatch.setattr(cfg.settings, "interview_async_submit", False)
    monkeypatch.setattr(_retry, "call_with_fallback", _stall_forever)

    tenant_id, headers = get_test_auth(new_tenant_id())
    enable = await client.put(
        f"/api/v1/settings?tenant_id={tenant_id}",
        json={"interviewer": {"enabled": True}},
        headers=headers,
    )
    assert enable.status_code == 200, enable.text

    base = datetime(2026, 9, 24, 8, 0, tzinfo=UTC)
    with caplog.at_level(logging.WARNING):
        resp = await client.post(
            "/api/v1/interview/submit",
            json={
                "tenant_id": tenant_id,
                "node_id": f"node-{uid()}",
                "agent_id": f"agent-{uid()}",
                "command_id": "cmd-1",
                "cursor_from": 0,
                "cursor_to": 10,
                "events": [
                    {
                        "seq": i,
                        "ts": (base + timedelta(minutes=i)).isoformat(),
                        "session_id": "sess-1",
                        "role": "assistant",
                        "kind": "message",
                        "content": f"Worked on step {i}: refactored the ingest pipeline.",
                    }
                    for i in range(3)
                ],
            },
            headers=headers,
        )

    assert resp.status_code == 504, resp.text
    err = resp.json()["error"]
    assert err["code"] == "REQUEST_BUDGET_EXCEEDED", (
        "a 504 from our own deadline must not claim an upstream timed out"
    )
    assert err["details"]["phase"] == "interview.chunk"
    assert err["details"]["path"] == "/api/v1/interview/submit"
    assert err["details"]["budget_seconds"] == 0.4
    assert "interview.chunk" in err["message"]

    rec = _budget_log(caplog, "interview exceeded its request budget")
    assert rec.phase == "interview.chunk"


# ===========================================================================
# 4 — the MCP transport, which bypasses the middleware entirely
# ===========================================================================


def _wire_recall(monkeypatch):
    """The storage-routed deps ``caura_recall`` resolves on ``mcp_server``.

    ``mcp_env`` already bypasses auth, trust and metering; recall additionally
    reads the tenant config and looks the agent up through the storage client.
    """
    from types import SimpleNamespace

    from core_api import mcp_server
    from tests._mcp_test_helpers import stub_storage_client

    async def _config(_tenant_id):
        return SimpleNamespace(
            require_agent_approval=False,
            recall_boost=False,
            graph_expand=False,
            entity_retrieval=True,
        )

    monkeypatch.setattr(mcp_server, "resolve_config", _config)
    stub_storage_client(monkeypatch, get_agent=None)


async def test_an_mcp_tool_call_records_the_hop_its_failure_came_out_of(
    mcp_env, monkeypatch, caplog
):
    """MCP is the surface agents actually use, and this mount is skipped by
    the timeout middleware — so ``phase()`` was a no-op on every hop the REST
    path names. Three defects have now been fixed on REST and never carried
    to MCP; attribution is not becoming the fourth.

    There is no deadline here to blow (see ``mcp_server``'s note), so the
    thing that has to carry the phase is the failure path: a storage read that
    gives up, or a client that disconnects and cancels the call.
    """
    from core_api import mcp_server

    _wire_recall(monkeypatch)

    async def _fail_in_storage(*_a, **_kw):
        from core_api.request_phase import phase

        with phase("storage.POST /memories/scored-search"):
            raise RuntimeError("storage read gave up")

    monkeypatch.setattr(mcp_server, "search_memories", _fail_in_storage)

    with caplog.at_level(logging.WARNING):
        # The SDK wraps a handler exception in its own error type, so the
        # concrete class is the SDK's business; what this pins is that the
        # phase survived to the log line regardless of what came out.
        with pytest.raises(Exception):  # noqa: B017
            await mcp_server.mcp.call_tool(
                "caura_recall", {"query": "who runs fleet ops"}
            )

    rec = _budget_log(caplog, "MCP tool call failed")
    assert rec.phase == "storage.POST /memories/scored-search"
    assert rec.tool == "caura_recall"


async def test_a_cancelled_mcp_tool_call_still_names_its_hop(
    mcp_env, monkeypatch, caplog
):
    """A hung MCP call ends when the client gives up and the connection drops,
    which cancels the dispatch. ``CancelledError`` is not an ``Exception``, so
    an ``except Exception`` here would let the one failure shaped exactly like
    h-01 pass through unattributed — and an unbounded ``slot_acquire`` wait is
    the h-01 shape this transport is MOST able to produce, since it has no
    request budget to cap it."""
    from core_api import mcp_server

    _wire_recall(monkeypatch)

    async def _cancel_in_slot(*_a, **_kw):
        from core_api.request_phase import phase

        with phase("slot_acquire.storage_search"):
            raise asyncio.CancelledError

    monkeypatch.setattr(mcp_server, "search_memories", _cancel_in_slot)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(asyncio.CancelledError):
            await mcp_server.mcp.call_tool(
                "caura_recall", {"query": "who runs fleet ops"}
            )

    rec = _budget_log(caplog, "MCP tool call failed")
    assert rec.phase == "slot_acquire.storage_search"
    assert rec.error_type == "CancelledError"


async def test_the_mcp_recorder_does_not_leak_between_tool_calls(mcp_env, monkeypatch):
    """Under the in-process transport every call shares one context, so a
    leaked binding would put one tool call's phases in the next one's report —
    attribution that is worse than none. Same hazard the REST recorder is
    pinned against, on a path that arms its own."""
    from core_api import mcp_server, request_phase

    _wire_recall(monkeypatch)

    async def _empty(*_a, **_kw):
        from core_api.request_phase import phase

        with phase("storage.POST /memories/scored-search"):
            return []

    monkeypatch.setattr(mcp_server, "search_memories", _empty)

    assert request_phase.current() is None
    await mcp_server.mcp.call_tool("caura_recall", {"query": "q"})
    assert request_phase.current() is None
