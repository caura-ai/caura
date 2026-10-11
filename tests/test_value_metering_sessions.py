"""Value metering, session side: where a request's session comes from, and what is counted per session.

MCP is stateless, so the server issues a session label on the first response
and reads it back; REST takes ``X-Caura-Session-Id``. The tracker counts calls
per session and the calls since its previous write. A write records its origin
under the broker's session when it stamped one, else the request's; a request
with no session records nothing.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core_api import session_context as sc
from core_api.services import session_tracker, value_ledger


async def _drive(app, headers: list[tuple[bytes, bytes]]):
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await app({"type": "http", "headers": headers}, receive, send)
    return sent


async def _ok_app(scope, receive, send):
    seen["session"] = sc.current_session_id()
    seen["source"] = sc.current_session_source()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"{}"})


seen: dict = {}


async def test_mcp_issues_a_session_on_a_request_that_had_none():
    sent = await _drive(lambda s, r, se: sc.run_with_mcp_session(s, r, se, _ok_app), [])
    issued = dict(sent[0]["headers"])[sc.MCP_SESSION_HEADER].decode()
    assert issued.startswith("mcp-") and seen == {"session": issued, "source": "mcp"}


async def test_mcp_reads_back_the_session_and_issues_no_new_one():
    sent = await _drive(
        lambda s, r, se: sc.run_with_mcp_session(s, r, se, _ok_app),
        [(sc.MCP_SESSION_HEADER, b"mcp-abc")],
    )
    assert sc.MCP_SESSION_HEADER not in dict(sent[0]["headers"])
    assert seen["session"] == "mcp-abc"


async def test_a_malformed_session_header_is_replaced_not_trusted():
    await _drive(
        lambda s, r, se: sc.run_with_mcp_session(s, r, se, _ok_app),
        [(sc.MCP_SESSION_HEADER, b"bad value;drop")],
    )
    assert seen["session"].startswith("mcp-") and seen["session"] != "bad value;drop"


async def test_rest_takes_the_header_and_clears_it_otherwise():
    mw = sc.SessionHeaderMiddleware(_ok_app)
    await _drive(mw, [(sc.REST_SESSION_HEADER, b"run-42")])
    assert seen == {"session": "run-42", "source": "rest"}
    await _drive(mw, [])
    assert seen == {"session": None, "source": None}


def test_the_tracker_counts_calls_and_resets_the_count_at_each_write():
    t = session_tracker.SessionTracker()
    for _ in range(3):
        t.touch("t1", "s1", "mcp", "a1")
    t.touch("t2", "s9", "rest", None)
    assert t.take_calls_since_write("t1", "s1") == 3
    t.touch("t1", "s1", "mcp", None)
    assert t.take_calls_since_write("t1", "s1") == 1
    drained = t._drain()
    assert set(drained) == {"t1", "t2"}
    assert drained["t1"][0]["calls"] == 4 and drained["t1"][0]["agent_id"] == "a1"
    # Flushed calls are not sent twice.
    t.touch("t1", "s1", "mcp", None)
    assert t._drain()["t1"][0]["calls"] == 1


def test_a_call_without_a_tenant_or_session_is_not_tracked():
    t = session_tracker.SessionTracker()
    t.touch(None, "s1", "mcp", None)
    t.touch("t1", None, "mcp", None)
    assert t._drain() == {}


@pytest.fixture
def scheduled(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        value_ledger,
        "track_task",
        lambda coro: (calls.append(coro.cr_frame.f_locals), coro.close()),
    )
    return calls


def test_a_write_records_its_origin_under_the_brokers_session_first(scheduled):
    sc.set_session("mcp-123", "mcp")
    memory = SimpleNamespace(
        id="m1",
        tenant_id="t1",
        agent_id="a1",
        metadata={"session_id": "broker-9"},
        created_at=None,
    )
    value_ledger.schedule_origin(
        memory,
        origin={"tokens_in": 500, "tokens_out": 80, "turns": 6, "source": "hook"},
    )
    row = scheduled[0]["row"]
    assert row["session_id"] == "broker-9"
    assert (row["origin_tokens_in"], row["origin_turns"], row["origin_source"]) == (
        500,
        6,
        "hook",
    )


def test_a_write_in_an_mcp_session_records_that_session(scheduled):
    sc.set_session("mcp-123", "mcp")
    value_ledger.schedule_origin(
        SimpleNamespace(
            id="m1", tenant_id="t1", agent_id="a1", metadata={}, created_at=None
        )
    )
    assert scheduled[0]["row"]["session_id"] == "mcp-123"
    assert scheduled[0]["row"]["session_source"] == "mcp"


def test_no_session_means_no_origin_and_no_reuse(scheduled):
    sc.set_session(None, None)
    value_ledger.schedule_origin(
        SimpleNamespace(
            id="m1", tenant_id="t1", agent_id="a1", metadata={}, created_at=None
        )
    )
    value_ledger.schedule_reuse(
        [SimpleNamespace(id="m1")], tenant_id="t1", reader_agent_id="a2"
    )
    assert scheduled == []


def test_a_recall_in_a_session_schedules_its_reuse(scheduled):
    sc.set_session("mcp-7", "mcp")
    value_ledger.schedule_reuse(
        [SimpleNamespace(id="m1"), SimpleNamespace(id="m2")],
        tenant_id="t1",
        reader_agent_id="a2",
    )
    body = scheduled[0]["body"]
    assert (
        body["session_id"] == "mcp-7"
        and body["memory_ids"] == ["m1", "m2"]
        and body["reader_agent_id"] == "a2"
    )


def test_the_mcp_write_tool_does_not_offer_origin():
    """A model cannot observe its own token use, so the MCP write takes none.

    The tool's parameters come from its handler's signature (tools/_types.py).
    """
    import inspect

    from core_api.tools.caura_write import _SPEC

    params = set(inspect.signature(_SPEC.handler).parameters)
    assert "content" in params
    assert not {p for p in params if "origin" in p or "token" in p}
