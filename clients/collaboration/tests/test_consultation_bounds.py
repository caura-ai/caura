"""Cyclic delegation ends with a useful failure within the consultation budget.

Agents run the real MCP dispatch against one in-memory Caura stand-in, so leases,
correlation and collection behave as in the supported stdio workflow. Each agent
uses a deliberately naive policy: "if I cannot answer, ask a peer and wait; on no
reply, ask again". Without bounds that policy waits forever in a cycle.
"""

import asyncio
import itertools
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from caura_bus_core import (
    AgentConfig,
    Bus,
    ConsultationBudget,
    ConsultationCycleError,
    ConsultationLimitError,
)
from caura_bus_core.consult import ROOT_SCOPE
from caura_bus_mcp.server import AppContext, mcp
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError


class Caura:
    """Just enough of the bus routes for send, wait, reply, ack and collect."""

    def __init__(self):
        self.ids = itertools.count(1)
        self.messages: dict[str, dict] = {}
        self.deliveries: list[dict] = []

    def _send(self, sender, to, body, kind, thread_id=None, correlation_id=None):
        message_id = f"msg_{next(self.ids)}"
        if correlation_id:
            thread_id = self.messages[correlation_id]["thread_id"]
        envelope = {
            "id": message_id,
            "from": sender,
            "to": to,
            "kind": kind,
            "thread_id": thread_id or f"t_{message_id}",
            "ts": 1,
            "body": body,
            "correlation_id": correlation_id,
        }
        self.messages[message_id] = envelope
        for recipient in to:
            self.deliveries.append(
                {
                    "id": f"d_{next(self.ids)}",
                    "message_id": message_id,
                    "recipient": recipient,
                    "state": "pending",
                    "reply_state": "awaiting" if kind == "request" else None,
                    "reply_message_id": None,
                }
            )
        if kind == "response":
            for row in self.deliveries:
                if row["message_id"] == correlation_id and row["recipient"] == sender:
                    row.update(reply_state="replied", reply_message_id=message_id)
        return envelope

    def _claim(self, row):
        row["state"] = "leased"
        due = datetime.now(UTC) + timedelta(seconds=60)
        return {
            "delivery_id": row["id"],
            "lease_token": "token",
            "lease_expires_at": due.isoformat(),
            "attempt": 1,
            "envelope": self.messages[row["message_id"]],
            "state": "leased",
            "processing_deadline": due.isoformat(),
        }

    def transport(self, agent):
        async def handle(request):
            path = request.url.path.removeprefix("/api/v1/bus/")
            body = json.loads(request.content) if request.content else {}
            if request.method == "POST" and path == "messages":
                env = self._send(
                    agent, body["to"], body["body"], body["kind"], body.get("thread_id"), body.get("reply_to")
                )
                return httpx.Response(
                    202,
                    json={"message_id": env["id"], "thread_id": env["thread_id"], "recipients": env["to"]},
                )
            if request.method == "GET" and path == "messages":
                reply_to = request.url.params.get("reply_to")
                rows = [
                    m
                    for m in reversed(self.messages.values())
                    if agent in m["to"] and m["kind"] == "response" and m["correlation_id"] == reply_to
                ]
                return httpx.Response(200, json={"messages": rows, "next_cursor": None})
            if request.method == "GET" and path.startswith("messages/"):
                message = self.messages[path.split("/")[1]]
                rows = [d for d in self.deliveries if d["message_id"] == message["id"]]
                return httpx.Response(200, json={"envelope": message, "deliveries": rows})
            if path == "inbox/wait":
                mine = [
                    d
                    for d in self.deliveries
                    if d["recipient"] == agent and d["state"] in {"pending", "leased"}
                ]
                return httpx.Response(
                    200, json={"delivery": self._claim(mine[0]) if mine else None, "notices": []}
                )
            delivery_id, action = path.removeprefix("deliveries/").split("/")
            row = next(d for d in self.deliveries if d["id"] == delivery_id)
            if action == "reply":
                request_message = self.messages[row["message_id"]]
                env = self._send(
                    agent, [request_message["from"]], body["body"], "response", None, row["message_id"]
                )
                if body.get("ack", True):
                    row["state"] = "acked"
                return httpx.Response(200, json={"message_id": env["id"]})
            if action == "ack":
                row["state"] = "acked"
                return httpx.Response(200, json={"state": "acked"})
            if action == "observe":
                return httpx.Response(200, json={"delivery": self._claim(row) | {"state": row["state"]}})
            if action == "renew":
                return httpx.Response(200, json={"lease_expires_at": "2030-01-01T00:00:00Z"})
            return httpx.Response(404, json={"detail": path})

        return httpx.MockTransport(handle)


@pytest.fixture
async def network():
    caura = Caura()
    opened = []

    def agent(agent_id, *, max_requests=2, deadline_seconds=3.0):
        config = AgentConfig(
            api_url="https://caura.test",
            agent={"agent_id": agent_id, "tenant_id": "tenant"},
            peers=["*"],
            consultation={"max_requests": max_requests, "deadline_seconds": deadline_seconds},
        )
        bus = Bus(config, api_key="key", transport=caura.transport(agent_id))
        app = AppContext(config, bus)
        opened.append(app)
        context = Context(request_context=SimpleNamespace(lifespan_context=app), mcp_server=mcp)

        async def call(op, args=None):
            result = await mcp.call_tool("peer", {"op": op, "args": args or {}}, context=context)
            return json.loads(result.content[0].text)

        return call

    try:
        yield caura, agent
    finally:
        for app in opened:
            await app.delivery.close()
            await app.bus.close()


async def delegate(call, peer, question="What is the release code?"):
    """Naive policy: ask, wait a bounded time, ask again on no reply."""
    for attempt in range(20):  # far above any budget; the bound must come from the client
        try:
            receipt = await call(
                "send",
                {"to": [peer], "body": question, "kind": "request", "idempotency_key": f"ask-{attempt}"},
            )
        except ToolError as exc:
            return f"gave up: {exc}"
        result = await call("collect", {"message_id": receipt["message_id"], "timeout": 1})
        if result["answers"]:
            return result["answers"][0]["body"]
    return "unbounded"


async def serve_by_delegating(call, peer):
    while True:
        claimed = (await call("wait", {"timeout": 0}))["delivery"]
        if claimed:
            break
        await asyncio.sleep(0.02)
    answer = await delegate(call, peer)
    await call("reply", {"delivery_id": claimed["delivery_id"], "body": answer, "idempotency_key": "r"})
    return answer


async def test_direct_back_edge_is_refused_and_the_failure_reaches_the_asker(network):
    caura, agent = network
    a, b = agent("a"), agent("b")
    started = asyncio.get_running_loop().time()
    b_task = asyncio.create_task(serve_by_delegating(b, "a"))
    answer = await asyncio.wait_for(delegate(a, "b"), 10)
    b_answer = await asyncio.wait_for(b_task, 1)
    assert answer == b_answer and answer.startswith("gave up:")
    assert "consultation cycle: a is waiting on your reply to its request" in answer
    assert "Reply to its request instead" in answer
    assert asyncio.get_running_loop().time() - started < 5
    # B's refused request was never sent: only A's request and B's reply exist.
    assert [m["kind"] for m in caura.messages.values()] == ["request", "response"]


async def test_longer_cycle_ends_within_each_hops_budget(network):
    caura, agent = network
    # A→B→C→A: C's request to A is not a direct back-edge, and A is busy collecting.
    a = agent("a", max_requests=3, deadline_seconds=8)
    b = agent("b", max_requests=3, deadline_seconds=6)
    c = agent("c", max_requests=2, deadline_seconds=4)
    started = asyncio.get_running_loop().time()
    tasks = [
        asyncio.create_task(serve_by_delegating(b, "c")),
        asyncio.create_task(serve_by_delegating(c, "a")),
    ]
    answer = await asyncio.wait_for(delegate(a, "b"), 12)
    b_answer, c_answer = await asyncio.wait_for(asyncio.gather(*tasks), 1)
    elapsed = asyncio.get_running_loop().time() - started
    # Every hop stops on its own bound with a model-readable reason; nothing waits forever.
    assert c_answer.startswith("gave up:") and "consultation budget spent: 2 requests" in c_answer
    assert b_answer.startswith("gave up:") or b_answer == c_answer
    assert answer.startswith("gave up:") or answer in {b_answer, c_answer}
    assert "consultation budget spent" in answer and "say which peers did not respond" in answer
    assert elapsed < 8  # within A's consultation deadline
    # C's two requests to A stay accepted and pending; collection cancelled nothing.
    to_a = [
        d
        for d in caura.deliveries
        if d["recipient"] == "a" and caura.messages[d["message_id"]]["kind"] == "request"
    ]
    assert len(to_a) == 2 and all(d["state"] == "pending" for d in to_a)


async def test_collect_is_clamped_to_the_remaining_consultation_time(network):
    _, agent = network
    a = agent("a", max_requests=1, deadline_seconds=0.5)
    agent("b")
    receipt = await a("send", {"to": ["b"], "body": "q", "kind": "request", "idempotency_key": "k"})
    started = asyncio.get_running_loop().time()
    result = await a("collect", {"message_id": receipt["message_id"], "timeout": 30})
    assert asyncio.get_running_loop().time() - started < 2
    assert result["outcome"] == "no_reply" and result["consultation_seconds_left"] < 0.5
    assert "Consultation time for this task is spent" in result["summary"]
    # Resending the same idempotent request does not spend budget; a new one does.
    await a("send", {"to": ["b"], "body": "q", "kind": "request", "idempotency_key": "k"})
    with pytest.raises(ToolError, match="consultation"):
        await a("send", {"to": ["b"], "body": "q2", "kind": "request", "idempotency_key": "k2"})


def test_budget_scopes_count_deadline_release_and_root_window():
    now = [0.0]
    budget = ConsultationBudget(max_requests=2, deadline_seconds=10, clock=lambda: now[0])
    budget.admit("d1", ["b"], deadline_in=5)
    budget.admit("d1", ["c"])
    with pytest.raises(ConsultationLimitError, match="budget spent"):
        budget.admit("d1", ["e"])
    assert budget.remaining("d1") == 5
    budget.release("d1")
    budget.admit("d1", ["b"])  # a finished delivery's scope is forgotten
    with pytest.raises(ConsultationCycleError):
        budget.admit("d2", ["a"], waiting_sender="a")
    budget.admit(ROOT_SCOPE, ["b"])
    now[0] = 11
    with pytest.raises(ConsultationLimitError, match="deadline passed"):
        budget.admit("d1", ["c"])
    budget.admit(ROOT_SCOPE, ["b"])  # the root scope opens a fresh window
    budget.admit(ROOT_SCOPE, ["c"])
    with pytest.raises(ConsultationLimitError):
        budget.admit(ROOT_SCOPE, ["e"])
    with pytest.raises(ValueError):
        ConsultationBudget(max_requests=0)
