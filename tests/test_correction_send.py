"""Correction propagation, CP-11 and CP-15: sending a correction's notices, and who confirmed.

``core_api.services.correction_sender`` posts one notice per recipient to the
Caura bus's system sender and maps each answer to a ``notice_state`` and a
``channel``; a status read refreshes the bus outcomes. The unit tests run it
against an in-memory storage and a scripted bus. The integration test drives
preview → send → confirm through the REST routes on a live database, with no
bus configured (standalone OSS: every notice goes to the recall channel) and
then with a scripted bus.

See ``docs/plans/correction-propagation.md``.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from core_api.services import correction_sender as cs
from tests.conftest import get_admin_headers, uid
from tests.test_correction_recipients import _PADDING, _settle
from tests.test_correction_recipients import as_agent as as_agent  # pytest fixture
from tests.test_correction_recipients import bus as bus  # pytest fixture

_STATES = ("pending", "sent", "delivered", "confirmed", "late", "failed", "unreachable")


def _recipient(agent_id: str) -> dict:
    return {
        "agent_id": agent_id,
        "reads": 1,
        "first_seen": "2026-11-05T09:00:00+00:00",
        "last_seen": "2026-11-05T09:00:00+00:00",
        "last_trace_id": None,
        "channel": None,
        "notice_state": "pending",
        "confirmed_at": None,
        "notified_at": None,
        "next_attempt_at": None,
        "attempts": 0,
        "bus_message_id": None,
        "bus_delivery_id": None,
    }


class _FakeStorage:
    """The four correction calls the sender makes, over one in-memory record."""

    def __init__(
        self,
        agents: list[str],
        *,
        kind: str = "content_edit",
        detail: dict | None = None,
    ) -> None:
        self.record: dict[str, Any] = {
            "id": "9b2c4c8e-0d43-4d9a-9a52-3f0f1f6e0a11",
            "memory_id": "mem-1",
            "kind": kind,
            "detail": detail or {},
            "actor": {"kind": "user", "id": "u1"},
            "corrected_at": "2026-11-05T10:00:00+00:00",
            "old_summary": "Tuesday",
            "new_summary": "Thursday",
            "status": "preview",
            "bus_checked_at": None,
            "recipients": [_recipient(a) for a in agents],
        }
        self.writes = 0

    def _out(self) -> dict:
        out = copy.deepcopy(self.record)
        out["counts"] = {
            s: sum(1 for r in out["recipients"] if r["notice_state"] == s)
            for s in _STATES
        }
        return out

    async def begin_correction_send(
        self, tenant_id: str, correction_id: str, *, sent_by: str
    ) -> dict:
        if self.record["status"] == "preview":
            self.record["status"] = "sending"
        return self._out()

    async def get_memory_correction(self, tenant_id: str, correction_id: str) -> dict:
        return self._out()

    async def apply_correction_states(
        self, tenant_id, correction_id, *, recipients, status=None, bus_checked_at=None
    ) -> dict:
        self.writes += 1
        for u in recipients:
            r = next(
                x for x in self.record["recipients"] if x["agent_id"] == u["agent_id"]
            )
            if r["notice_state"] == "confirmed" and u["notice_state"] != "confirmed":
                continue
            r.update(u)
        if status is not None and self.record["status"] != "preview":
            self.record["status"] = status
        if bus_checked_at is not None:
            self.record["bus_checked_at"] = bus_checked_at
        return self._out()

    def state(self, agent_id: str) -> dict:
        return next(r for r in self.record["recipients"] if r["agent_id"] == agent_id)


class _FakeBus:
    """Scripted answers to the system-send and status calls, recording each."""

    def __init__(self, answer, status: dict | None = None) -> None:
        self.answer = answer
        self.status = status
        self.sends: list[list[str]] = []
        self.bodies: list[dict] = []
        self.status_reads = 0

    async def post(self, tenant_id: str, correction: dict, batch: list[dict]) -> dict:
        agents = [r["agent_id"] for r in batch]
        self.sends.append(agents)
        self.bodies.append(
            {"kind": cs.bus_kind(correction), "by": cs.corrected_by(correction)}
        )
        return self.answer(agents, len(self.sends))

    async def get(self, tenant_id: str, correction_id: str) -> dict | None:
        self.status_reads += 1
        return self.status


class _Waits:
    def __init__(self) -> None:
        self.slept: list[float] = []
        self.scheduled: list[tuple[str, float]] = []


@pytest.fixture
def wire(monkeypatch):
    def _wire(storage: _FakeStorage, bus: _FakeBus | None) -> _Waits:
        monkeypatch.setattr(cs, "get_storage_client", lambda: storage)
        monkeypatch.setattr(
            cs.settings, "collaboration_api_url", "http://bus.test" if bus else ""
        )
        if bus is not None:
            monkeypatch.setattr(cs, "_post_bus", bus.post)
            monkeypatch.setattr(cs, "_bus_status", bus.get)
        waits = _Waits()

        async def _sleep(seconds: float) -> None:
            waits.slept.append(seconds)

        monkeypatch.setattr(cs.asyncio, "sleep", _sleep)
        monkeypatch.setattr(
            cs, "_schedule_retry", lambda t, c, w: waits.scheduled.append((c, w))
        )
        return waits

    return _wire


def _accept(
    agents: list[str], _call: int, unknown: frozenset[str] = frozenset()
) -> dict:
    return {
        "recipients": [
            {"agent_id": a, "status": "unknown_agent"}
            if a in unknown
            else {
                "agent_id": a,
                "status": "accepted",
                "message_id": f"msg_{a}",
                "delivery_id": f"dlv_{a}",
            }
            for a in agents
        ],
        "retry_after_seconds": None,
    }


async def test_without_a_bus_every_notice_goes_to_recall(wire) -> None:
    storage = _FakeStorage(["a", "b"])
    wire(storage, None)
    out = await cs.send_correction("t1", storage.record["id"], sent_by="user:u1")
    assert out is not None
    assert out["status"] == "sent"
    assert {(r["channel"], r["notice_state"]) for r in out["recipients"]} == {
        ("recall", "pending")
    }


async def test_bus_answers_map_to_states_and_channels(wire) -> None:
    storage = _FakeStorage(
        ["a", "ghost"], kind="status", detail={"new_status": "outdated"}
    )
    bus = _FakeBus(lambda agents, n: _accept(agents, n, unknown=frozenset({"ghost"})))
    wire(storage, bus)
    out = await cs.send_correction("t1", storage.record["id"], sent_by="user:u1")
    assert out is not None and out["status"] == "sent"
    a, ghost = storage.state("a"), storage.state("ghost")
    assert (a["channel"], a["notice_state"], a["bus_delivery_id"], a["attempts"]) == (
        "bus",
        "sent",
        "dlv_a",
        1,
    )
    assert a["notified_at"] is not None
    # Not on the bus: waiting for its next recall, not unreachable.
    assert (ghost["channel"], ghost["notice_state"]) == ("recall", "pending")
    assert bus.bodies[0] == {"kind": "status_outdated", "by": "user:u1"}


async def test_recipients_go_in_batches_of_at_most_200(wire) -> None:
    storage = _FakeStorage([f"agent-{i:03d}" for i in range(250)])
    bus = _FakeBus(_accept)
    wire(storage, bus)
    await cs.send_correction("t1", storage.record["id"], sent_by="k")
    assert [len(b) for b in bus.sends] == [200, 50]


async def test_a_short_throttle_is_retried_inline(wire) -> None:
    storage = _FakeStorage(["a", "b"])

    def answer(agents, call):
        if call == 1:
            return {
                "recipients": [
                    {
                        "agent_id": "a",
                        "status": "accepted",
                        "message_id": "m",
                        "delivery_id": "d",
                    },
                    {"agent_id": "b", "status": "throttled"},
                ],
                "retry_after_seconds": 2,
            }
        return _accept(agents, call)

    bus = _FakeBus(answer)
    waits = wire(storage, bus)
    out = await cs.send_correction("t1", storage.record["id"], sent_by="k")
    assert out is not None and out["status"] == "sent"
    # The second call carried only the throttled recipient, after the wait the bus asked for.
    assert bus.sends == [["a", "b"], ["b"]]
    assert waits.slept == [2.0]
    assert storage.state("b")["attempts"] == 2


async def test_a_long_throttle_stays_pending_for_a_background_retry(wire) -> None:
    storage = _FakeStorage(["a"])
    bus = _FakeBus(
        lambda agents, n: {
            "recipients": [{"agent_id": "a", "status": "backlog_full"}],
            "retry_after_seconds": 60,
        }
    )
    waits = wire(storage, bus)
    out = await cs.send_correction("t1", storage.record["id"], sent_by="k")
    assert out is not None and out["status"] == "sending"
    a = storage.state("a")
    assert (a["channel"], a["notice_state"]) == ("bus", "pending")
    assert a["next_attempt_at"] is not None
    assert waits.slept == []
    assert waits.scheduled == [(storage.record["id"], 60.0)]
    # Sending again before it is due calls nothing: the record comes back as it is.
    again = await cs.send_correction("t1", storage.record["id"], sent_by="k")
    assert again is not None and again["status"] == "sending"
    assert len(bus.sends) == 1


async def test_a_bus_that_refuses_falls_back_to_recall(wire) -> None:
    storage = _FakeStorage(["a"])
    wire(storage, _FakeBus(lambda agents, n: {"refused": 403}))
    out = await cs.send_correction("t1", storage.record["id"], sent_by="k")
    assert out is not None and out["status"] == "sent"
    assert (storage.state("a")["channel"], storage.state("a")["notice_state"]) == (
        "recall",
        "pending",
    )


async def test_a_bus_that_does_not_answer_leaves_the_notice_pending(wire) -> None:
    storage = _FakeStorage(["a"])

    def down(agents, call):
        raise cs.BusUnavailable("HTTP 503")

    waits = wire(storage, _FakeBus(down))
    out = await cs.send_correction("t1", storage.record["id"], sent_by="k")
    assert waits.scheduled == [(storage.record["id"], cs.DEFAULT_RETRY_AFTER_S)]
    assert out is not None and out["status"] == "sending"
    assert storage.state("a")["notice_state"] == "pending"
    assert storage.state("a")["next_attempt_at"] is not None


async def test_a_sent_correction_is_returned_unchanged(wire) -> None:
    storage = _FakeStorage(["a"])
    storage.record["status"] = "sent"
    bus = _FakeBus(_accept)
    wire(storage, bus)
    out = await cs.send_correction("t1", storage.record["id"], sent_by="k")
    assert out is not None and out["status"] == "sent"
    assert bus.sends == []


async def test_a_status_read_maps_outcomes_and_is_cached(wire) -> None:
    storage = _FakeStorage(["a", "b", "c"])
    bus = _FakeBus(_accept)
    wire(storage, bus)
    await cs.send_correction("t1", storage.record["id"], sent_by="k")
    bus.status = {
        "recipients": [
            {
                "agent_id": "a",
                "outcome": "confirmed",
                "replied_at": "2026-11-05T11:00:00Z",
            },
            {"agent_id": "b", "outcome": "delivered"},
            {"agent_id": "c", "outcome": "late"},
        ]
    }
    out = await cs.refresh_correction(
        "t1", await storage.get_memory_correction("t1", "x")
    )
    assert [r["notice_state"] for r in out["recipients"]] == [
        "confirmed",
        "delivered",
        "late",
    ]
    assert storage.state("a")["confirmed_at"] == "2026-11-05T11:00:00Z"
    assert out["counts"]["confirmed"] == 1 and out["status"] == "sent"
    # Within 30 s the next read does not ask the bus again.
    await cs.refresh_correction("t1", await storage.get_memory_correction("t1", "x"))
    assert bus.status_reads == 1
    # Past it, and every recipient settled: the correction closes.
    storage.record["bus_checked_at"] = (
        datetime.now(UTC) - timedelta(seconds=31)
    ).isoformat()
    bus.status = {
        "recipients": [
            {
                "agent_id": "b",
                "outcome": "confirmed",
                "acked_at": "2026-11-05T12:00:00Z",
            },
            {"agent_id": "c", "outcome": "failed"},
        ]
    }
    out = await cs.refresh_correction(
        "t1", await storage.get_memory_correction("t1", "x")
    )
    assert out["status"] == "closed"
    assert bus.status_reads == 2


async def test_a_status_read_never_undoes_a_recall_confirm(wire) -> None:
    storage = _FakeStorage(["a"])
    bus = _FakeBus(_accept)
    wire(storage, bus)
    await cs.send_correction("t1", storage.record["id"], sent_by="k")
    storage.state("a").update(
        notice_state="confirmed", confirmed_at="2026-11-05T11:00:00Z"
    )
    bus.status = {"recipients": [{"agent_id": "a", "outcome": "sent"}]}
    out = await cs.refresh_correction(
        "t1", await storage.get_memory_correction("t1", "x")
    )
    assert out["recipients"][0]["notice_state"] == "confirmed"
    assert bus.status_reads == 0


async def test_a_read_closes_a_correction_every_recipient_confirmed(wire) -> None:
    storage = _FakeStorage(["a", "b"])
    wire(storage, None)
    await cs.send_correction("t1", storage.record["id"], sent_by="k")
    storage.state("a").update(notice_state="confirmed")
    out = await cs.refresh_correction(
        "t1", await storage.get_memory_correction("t1", "x")
    )
    assert out["status"] == "sent"
    storage.state("b").update(notice_state="confirmed")
    out = await cs.refresh_correction(
        "t1", await storage.get_memory_correction("t1", "x")
    )
    assert out["status"] == "closed"


# ── integration: preview → send → confirm over REST ───────────────────────


async def _corrected_memory(
    client, tenant_id, as_agent, bus, monkeypatch
) -> tuple[str, str]:
    """reader-a and reader-b read a memory; editor (trust 3) edits it. Returns (memory_id, correction_id)."""
    from core_api.services import agent_service

    real_lookup = agent_service.lookup_agent

    async def lookup(tid, agent_id, *, read=True):
        if agent_id in ("editor", "sender"):
            return {"agent_id": agent_id, "trust_level": 3}
        if agent_id == "junior":
            return {"agent_id": agent_id, "trust_level": 1}
        return await real_lookup(tid, agent_id, read=read)

    monkeypatch.setattr(agent_service, "lookup_agent", lookup)
    w = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "agent_id": "writer",
            "memory_type": "fact",
            "content": f"The {uid()} deploy window is Tuesday." + _PADDING,
        },
        headers=get_admin_headers(),
    )
    assert w.status_code in (200, 201), w.text
    memory_id = w.json()["id"]
    for agent in ("reader-a", "reader-b", "editor"):
        as_agent(tenant_id, agent)
        assert (
            await client.get(f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}")
        ).status_code == 200
        await _settle()
    as_agent(tenant_id, "editor")
    r = await client.patch(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant_id}",
        json={"content": "The deploy window moved to Thursday." + _PADDING},
    )
    assert r.status_code == 200, r.text
    await _settle()
    await bus.drain()
    await _settle()
    as_agent(tenant_id, None)
    lst = await client.get(
        f"/api/v1/memories/{memory_id}/corrections?tenant_id={tenant_id}",
        headers=get_admin_headers(),
    )
    [correction] = lst.json()["corrections"]
    return memory_id, correction["id"]


@pytest.mark.integration
async def test_send_then_confirm_without_a_bus(
    client, tenant_id, as_agent, bus, monkeypatch
) -> None:
    monkeypatch.setattr(cs.settings, "collaboration_api_url", "")
    _, cid = await _corrected_memory(client, tenant_id, as_agent, bus, monkeypatch)

    # A trust-1 agent may not send.
    as_agent(tenant_id, "junior")
    r = await client.post(f"/api/v1/corrections/{cid}/send?tenant_id={tenant_id}")
    assert r.status_code == 403, r.text

    as_agent(tenant_id, "sender")
    r = await client.post(f"/api/v1/corrections/{cid}/send?tenant_id={tenant_id}")
    assert r.status_code == 200, r.text
    sent = r.json()
    assert sent["status"] == "sent"
    assert sent["sent_by"] == "agent:sender"
    assert {(x["channel"], x["notice_state"]) for x in sent["recipients"]} == {
        ("recall", "pending")
    }

    # Idempotent.
    again = await client.post(f"/api/v1/corrections/{cid}/send?tenant_id={tenant_id}")
    assert again.status_code == 200 and again.json()["sent_at"] == sent["sent_at"]

    as_agent(tenant_id, "reader-a")
    c = await client.post(f"/api/v1/corrections/{cid}/confirm?tenant_id={tenant_id}")
    assert c.status_code == 200, c.text
    assert c.json()["notice_state"] == "confirmed" and c.json()["confirmed_at"]

    as_agent(tenant_id, "stranger")
    assert (
        await client.post(f"/api/v1/corrections/{cid}/confirm?tenant_id={tenant_id}")
    ).status_code == 404
    # An agent credential keeps its own identity: naming a recipient does not
    # confirm for it.
    assert (
        await client.post(
            f"/api/v1/corrections/{cid}/confirm?tenant_id={tenant_id}&agent_id=reader-b"
        )
    ).status_code == 404

    # The plugin's shape: a tenant (here admin) key names the agent with agent_id.
    as_agent(tenant_id, None)
    url = f"/api/v1/corrections/{cid}/confirm?tenant_id={tenant_id}"
    assert (await client.post(url, headers=get_admin_headers())).status_code == 403
    assert (
        await client.post(f"{url}&agent_id=stranger", headers=get_admin_headers())
    ).status_code == 404
    b = await client.post(f"{url}&agent_id=reader-b", headers=get_admin_headers())
    assert b.status_code == 200, b.text
    assert (b.json()["agent_id"], b.json()["notice_state"]) == ("reader-b", "confirmed")

    view = await client.get(
        f"/api/v1/corrections/{cid}?tenant_id={tenant_id}", headers=get_admin_headers()
    )
    assert view.status_code == 200, view.text
    body = view.json()
    assert (body["counts"]["recipients"], body["counts"]["confirmed"]) == (2, 2)
    assert body["status"] == "closed"


@pytest.mark.integration
async def test_send_over_a_bus(client, tenant_id, as_agent, bus, monkeypatch) -> None:
    _, cid = await _corrected_memory(client, tenant_id, as_agent, bus, monkeypatch)
    fake = _FakeBus(
        lambda agents, n: _accept(agents, n, unknown=frozenset({"reader-b"}))
    )
    monkeypatch.setattr(cs.settings, "collaboration_api_url", "http://bus.test")
    monkeypatch.setattr(cs, "_post_bus", fake.post)
    monkeypatch.setattr(cs, "_bus_status", fake.get)

    r = await client.post(
        f"/api/v1/corrections/{cid}/send?tenant_id={tenant_id}",
        headers=get_admin_headers(),
    )
    assert r.status_code == 200, r.text
    by = {x["agent_id"]: x for x in r.json()["recipients"]}
    assert (by["reader-a"]["channel"], by["reader-a"]["notice_state"]) == (
        "bus",
        "sent",
    )
    assert (by["reader-b"]["channel"], by["reader-b"]["notice_state"]) == (
        "recall",
        "pending",
    )

    fake.status = {
        "recipients": [
            {
                "agent_id": "reader-a",
                "outcome": "confirmed",
                "acked_at": "2026-11-05T12:00:00Z",
            }
        ]
    }
    view = await client.get(
        f"/api/v1/corrections/{cid}?tenant_id={tenant_id}", headers=get_admin_headers()
    )
    assert view.status_code == 200, view.text
    body = view.json()
    assert body["counts"]["confirmed"] == 1 and body["counts"]["pending"] == 1
    assert body["counts"]["unreachable"] == 0
    assert body["status"] == "sent"


# ── Cloud Run: an ID token for a private collaboration service ─────────────


async def test_bus_calls_carry_a_cloud_run_id_token_for_an_https_bus(
    monkeypatch,
) -> None:
    audiences: list[str] = []

    async def _token(audience: str) -> dict[str, str]:
        audiences.append(audience)
        return {"Authorization": "Bearer id-token"}

    monkeypatch.setattr(cs, "fetch_auth_header", _token)
    monkeypatch.setattr(cs, "_bus_secret", lambda: "s3cret")
    monkeypatch.setattr(
        cs.settings,
        "collaboration_api_url",
        "https://staging-caura-collaboration-x.a.run.app/",
    )
    assert await cs._bus_headers() == {
        "X-Caura-System-Secret": "s3cret",
        "X-Serverless-Authorization": "Bearer id-token",
    }
    assert audiences == ["https://staging-caura-collaboration-x.a.run.app"]


async def test_no_id_token_off_cloud_run_or_over_plain_http(monkeypatch) -> None:
    async def _none(audience: str) -> dict[str, str]:
        return {}

    async def _never(audience: str) -> dict[str, str]:
        raise AssertionError("no token is fetched for an http bus")

    monkeypatch.setattr(cs, "_bus_secret", lambda: "s3cret")
    monkeypatch.setattr(cs.settings, "collaboration_api_url", "https://bus.example")
    monkeypatch.setattr(cs, "fetch_auth_header", _none)
    assert await cs._bus_headers() == {"X-Caura-System-Secret": "s3cret"}
    monkeypatch.setattr(
        cs.settings, "collaboration_api_url", "http://collaboration:8000"
    )
    monkeypatch.setattr(cs, "fetch_auth_header", _never)
    assert await cs._bus_headers() == {"X-Caura-System-Secret": "s3cret"}


async def test_a_send_posts_the_id_token_to_the_bus(monkeypatch) -> None:
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "recipients": [
                    {"agent_id": "a", "status": "accepted", "message_id": "m1"}
                ]
            },
        )

    real_client = httpx.AsyncClient

    def _client(**kwargs):
        return real_client(transport=httpx.MockTransport(_handler), **kwargs)

    async def _token(audience: str) -> dict[str, str]:
        return {"Authorization": "Bearer id-token"}

    monkeypatch.setattr(cs.httpx, "AsyncClient", _client)
    monkeypatch.setattr(cs, "fetch_auth_header", _token)
    monkeypatch.setattr(cs, "_bus_secret", lambda: "s3cret")
    monkeypatch.setattr(cs.settings, "collaboration_api_url", "https://bus.example")
    correction = {
        "id": "c1",
        "memory_id": "m",
        "kind": "content_edit",
        "corrected_at": "2026-11-05T12:00:00Z",
        "actor": {"kind": "user", "id": "u"},
    }
    answer = await cs._post_bus(
        "t1", correction, [{"agent_id": "a", "first_seen": "2026-11-05T11:00:00Z"}]
    )

    assert answer["recipients"][0]["status"] == "accepted"
    (req,) = seen
    assert req.headers["X-Serverless-Authorization"] == "Bearer id-token"
    assert req.headers["X-Caura-System-Secret"] == "s3cret"
    assert "Authorization" not in req.headers
