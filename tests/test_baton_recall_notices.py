"""HB-09 — handoff baton notices on recall, the core-api half.

A receiver that is not listening on the bus hears about a baton on its next
recall: ``baton_notices`` on MCP ``caura_recall`` and REST ``/search`` and
``/recall``. At most two, compact, absent when there are none (no new bytes on
the ordinary recall), never for a caller with no agent identity or a person,
and a failed lookup reads as none. It reads the baton with ``GET /batons/{id}``
(MCP ``caura_manage op=read_baton``) and accepts it with ``POST
/batons/{id}/accept`` (``op=accept_baton``); the bus state of a notice it was
shown is copied back after the response. Storage's half is
``core-storage-api/tests/test_baton_notices.py``.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core_api import mcp_server
from core_api.audit_actor import audit_principal
from core_api.config import settings
from core_api.schemas import BatonNotice, SearchResponse
from core_api.services import baton_notices as bn
from tests._legacy_contracts import LEGACY_API_KEY_FIELD
from tests._mcp_test_helpers import parse_envelope, stub_storage_client
from tests.conftest import get_admin_headers
from tests.test_correction_recipients import _PADDING, _settle
from tests.test_correction_recipients import as_agent as as_agent  # pytest fixture


def _row(i: int, **extra) -> dict:
    return {
        "baton_id": f"0000000{i}-0000-4000-8000-000000000000",
        "from_agent": "sender",
        "goal": f"Finish the csv-diff edge cases ({i})",
        "created_at": f"2026-11-0{i}T09:00:00+00:00",
        "message_id": f"msg-{i}",
        "bus_checked_at": None,
        **extra,
    }


@pytest.fixture
def storage(monkeypatch):
    sc = MagicMock(name="storage_client")
    sc.list_baton_notices = AsyncMock(return_value=[])
    sc.accept_baton = AsyncMock(return_value=None)
    sc.mark_baton_notices_checked = AsyncMock(return_value=0)
    monkeypatch.setattr(bn, "get_storage_client", lambda: sc)
    return sc


@pytest.fixture
def scheduled(monkeypatch):
    """Background coroutines the service schedules, run by the test."""
    coros: list = []
    monkeypatch.setattr(bn, "track_task", coros.append)
    return coros


@pytest.fixture
def no_bus(monkeypatch):
    monkeypatch.setattr(settings, "collaboration_api_url", "")


@pytest.fixture
def a_bus(monkeypatch):
    monkeypatch.setattr(settings, "collaboration_api_url", "http://bus.test")


# ---------------------------------------------------------------------------
# The service: who is looked up, and what a notice looks like
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_notices_are_compact_capped_and_carry_how_to_read_and_accept(storage):
    storage.list_baton_notices.return_value = [_row(i) for i in (1, 2, 3)]

    out = await bn.baton_notices("t-1", "agent-a", surface="mcp")

    storage.list_baton_notices.assert_awaited_once_with("t-1", "agent-a", limit=2)
    assert [n["baton_id"] for n in out] == [_row(1)["baton_id"], _row(2)["baton_id"]]
    assert set(out[0]) == {"baton_id", "from_agent", "goal", "created_at", "ask"}
    assert "op=read_baton" in out[0]["ask"] and "op=accept_baton" in out[0]["ask"]
    assert f"metadata.baton_id={_row(1)['baton_id']}" in out[0]["ask"]
    rest = await bn.baton_notices("t-1", "agent-a", surface="rest")
    assert f"/api/v1/batons/{_row(1)['baton_id']}/accept" in rest[0]["ask"]


@pytest.mark.unit
async def test_a_long_goal_is_cut(storage):
    storage.list_baton_notices.return_value = [_row(1, goal="x" * 2000)]
    [notice] = await bn.baton_notices("t-1", "a", surface="rest")
    assert len(notice["goal"]) == bn.GOAL_CHARS and notice["goal"].endswith("…")


@pytest.mark.unit
async def test_no_agent_identity_or_a_person_looks_nothing_up(storage):
    assert await bn.baton_notices("t-1", None, surface="rest") == []
    assert bn.start_notices("t-1", None, surface="rest") is None
    token = audit_principal.set({"credential_kind": "person"})
    try:
        assert await bn.baton_notices("t-1", "agent-a", surface="rest") == []
        assert bn.start_notices("t-1", "agent-a", surface="rest") is None
    finally:
        audit_principal.reset(token)
    storage.list_baton_notices.assert_not_awaited()


@pytest.mark.unit
async def test_a_failed_lookup_is_no_notices(storage, scheduled, a_bus):
    storage.list_baton_notices.side_effect = RuntimeError("storage down")
    assert (
        await bn.finish_notices(bn.start_notices("t-1", "agent-a", surface="rest"))
        == []
    )
    assert scheduled == []


# ---------------------------------------------------------------------------
# The bus owns the delivery state: copied back after a response that showed it
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_without_a_bus_nothing_is_checked(storage, scheduled, no_bus):
    storage.list_baton_notices.return_value = [_row(1)]
    assert (
        len(await bn.finish_notices(bn.start_notices("t-1", "a", surface="rest"))) == 1
    )
    assert scheduled == []


@pytest.mark.unit
async def test_a_bus_ack_or_reply_is_copied_back(
    storage, scheduled, a_bus, monkeypatch
):
    storage.list_baton_notices.return_value = [_row(1), _row(2)]
    states = {_row(1)["baton_id"]: "accepted", _row(2)["baton_id"]: "delivered"}
    asked: list[tuple] = []

    async def bus_state(tenant_id, agent_id, baton_id):
        asked.append((tenant_id, agent_id, baton_id))
        return states[baton_id]

    monkeypatch.setattr(bn, "_bus_state", bus_state)
    assert (
        len(await bn.finish_notices(bn.start_notices("t-1", "a", surface="rest"))) == 2
    )
    (coro,) = scheduled
    await coro

    assert [a[2] for a in asked] == [_row(1)["baton_id"], _row(2)["baton_id"]]
    storage.accept_baton.assert_awaited_once_with(
        "t-1", _row(1)["baton_id"], agent_id="a", via="bus", state="accepted"
    )
    storage.mark_baton_notices_checked.assert_awaited_once_with(
        "t-1", "a", [_row(1)["baton_id"], _row(2)["baton_id"]]
    )


@pytest.mark.unit
async def test_a_recent_check_or_no_bus_message_is_not_due(storage, scheduled, a_bus):
    recent = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    storage.list_baton_notices.return_value = [
        _row(1, bus_checked_at=recent),
        _row(2, message_id=None),
    ]
    assert (
        len(await bn.finish_notices(bn.start_notices("t-1", "a", surface="rest"))) == 2
    )
    assert scheduled == []


@pytest.mark.unit
async def test_a_bus_that_does_not_answer_is_logged_not_raised(
    storage, scheduled, a_bus, monkeypatch, caplog
):
    storage.list_baton_notices.return_value = [_row(1)]

    async def down(*_a):
        raise RuntimeError("bus down")

    monkeypatch.setattr(bn, "_bus_state", down)
    assert (
        len(await bn.finish_notices(bn.start_notices("t-1", "a", surface="rest"))) == 1
    )
    (coro,) = scheduled
    await coro
    assert "baton bus state not read" in caplog.text
    storage.accept_baton.assert_not_awaited()


@pytest.mark.unit
async def test_bus_state_reads_the_receivers_own_row(a_bus, monkeypatch):
    import httpx

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "receivers": [
                    {"agent_id": "b", "state": "delivered"},
                    {"agent_id": "a", "state": "replied"},
                ]
            },
        )

    real = httpx.AsyncClient
    monkeypatch.setattr(
        bn.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    monkeypatch.setattr(bn, "_bus_secret", lambda: "s3cret")
    assert await bn._bus_state("t-1", "a", "b-1") == "replied"
    (req,) = seen
    assert str(req.url).startswith("http://bus.test/api/v1/bus/system/batons/b-1?")
    assert req.url.params["agent_id"] == "a" and req.url.params["tenant_id"] == "t-1"
    assert req.headers["X-Caura-System-Secret"] == "s3cret"


# ---------------------------------------------------------------------------
# Accept and read, the service
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_accept_marks_the_row_and_leaves_the_bus_alone(storage, scheduled, a_bus):
    """An ack on the bus takes the receiver's own lease; the bus status reads the row instead."""
    storage.accept_baton.return_value = {
        "baton_id": "b-1",
        "agent_id": "a",
        "from_agent": "sender",
        "state": "accepted",
        "accepted_at": "2026-11-05T10:00:00+00:00",
        "message_id": "msg-1",
    }
    out = await bn.accept_as_agent("t-1", "b-1", "a")
    assert out == {
        "baton_id": "b-1",
        "agent_id": "a",
        "from_agent": "sender",
        "state": "accepted",
        "accepted_at": "2026-11-05T10:00:00+00:00",
    }
    storage.accept_baton.assert_awaited_once_with(
        "t-1", "b-1", agent_id="a", via="recall"
    )
    assert scheduled == []


@pytest.mark.unit
async def test_a_non_receiver_cannot_accept(storage):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await bn.accept_as_agent("t-1", "b-1", "stranger")
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# REST envelope: absent when empty, byte for byte
# ---------------------------------------------------------------------------


def _serve(response: SearchResponse) -> bytes:
    app = FastAPI()

    @app.get("/s", response_model=SearchResponse)
    async def _s():
        return response

    return TestClient(app).get("/s").content


@pytest.mark.unit
def test_search_payload_unchanged_without_notices():
    plain = _serve(SearchResponse(items=[], recall_tracked=False))
    assert b"baton_notices" not in plain
    assert (
        _serve(SearchResponse(items=[], recall_tracked=False, baton_notices=None))
        == plain
    )
    assert (
        _serve(SearchResponse(items=[], recall_tracked=False, baton_notices=[]))
        == plain
    )


@pytest.mark.unit
def test_search_payload_carries_notices_when_owed():
    row = _row(1)
    notice = BatonNotice(
        baton_id=row["baton_id"],
        from_agent="sender",
        goal="g",
        created_at=row["created_at"],
        ask="accept",
    )
    body = json.loads(_serve(SearchResponse(items=[], baton_notices=[notice])))
    assert body["baton_notices"] == [notice.model_dump()]


# ---------------------------------------------------------------------------
# MCP caura_recall and caura_manage
# ---------------------------------------------------------------------------


class _MemoryStub:
    def model_dump(self, mode: str = "python"):
        return {"id": "m-1"}


class _FakeConfig:
    recall_boost = False
    graph_expand = False
    entity_retrieval = True


async def _fake_resolve_config(tenant_id):
    return _FakeConfig()


@pytest.mark.unit
async def test_mcp_recall_carries_notices_on_any_query(mcp_env, monkeypatch, no_bus):
    mcp_env["service"]("search_memories").return_value = [_MemoryStub()]
    monkeypatch.setattr(mcp_server, "resolve_config", _fake_resolve_config)
    sc = stub_storage_client(monkeypatch, get_agent=None, list_baton_notices=[_row(1)])

    payload = parse_envelope(await mcp_server.caura_recall(query="something unrelated"))

    assert payload["count"] == 1
    (notice,) = payload["baton_notices"]
    assert notice["baton_id"] == _row(1)["baton_id"]
    assert "op=accept_baton" in notice["ask"]
    assert sc.list_baton_notices.await_args.kwargs == {"limit": 2}


@pytest.mark.unit
async def test_mcp_recall_omits_notices_when_none(mcp_env, monkeypatch):
    mcp_env["service"]("search_memories").return_value = [_MemoryStub()]
    monkeypatch.setattr(mcp_server, "resolve_config", _fake_resolve_config)
    stub_storage_client(monkeypatch, get_agent=None)

    payload = parse_envelope(await mcp_server.caura_recall(query="anything"))
    assert "baton_notices" not in payload


@pytest.mark.unit
async def test_mcp_manage_baton_ops_need_a_uuid(mcp_env):
    for op in ("read_baton", "accept_baton"):
        payload = parse_envelope(await mcp_server.caura_manage(op=op, baton_id="nope"))
        assert payload["error"]["code"] == "INVALID_ARGUMENTS"
        assert "baton_id" in payload["error"]["message"]


@pytest.mark.unit
async def test_mcp_manage_accept_baton_acts_as_the_caller(mcp_env, monkeypatch, no_bus):
    sc = stub_storage_client(
        monkeypatch,
        get_agent=None,
        accept_baton={
            "baton_id": _row(1)["baton_id"],
            "agent_id": "receiver",
            "from_agent": "sender",
            "state": "accepted",
            "accepted_at": "2026-11-05T10:00:00+00:00",
            "message_id": None,
        },
    )
    payload = parse_envelope(
        await mcp_server.caura_manage(
            op="accept_baton", baton_id=_row(1)["baton_id"], agent_id="receiver"
        )
    )
    assert payload["state"] == "accepted" and payload["agent_id"] == "receiver"
    sc.accept_baton.assert_awaited_once_with(
        mock.ANY, _row(1)["baton_id"], agent_id="receiver", via="recall"
    )


@pytest.mark.unit
async def test_mcp_manage_read_baton_refuses_a_non_receiver(mcp_env, monkeypatch):
    stub_storage_client(
        monkeypatch,
        get_agent=None,
        get_document={
            "data": {
                "from_agent": "sender",
                "recipients": [{"agent_id": "b", "memory_ids": []}],
            }
        },
    )
    payload = parse_envelope(
        await mcp_server.caura_manage(
            op="read_baton", baton_id=_row(1)["baton_id"], agent_id="stranger"
        )
    )
    assert payload["error"]["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# REST, whose agent: the OpenClaw plugin searches on the TENANT key
# ---------------------------------------------------------------------------

GATEWAY_SECRET = "test-gateway-secret"


@pytest.fixture
def owed(monkeypatch):
    monkeypatch.setattr(settings, "is_standalone", False)
    monkeypatch.setattr(settings, LEGACY_API_KEY_FIELD, "")
    monkeypatch.setattr(settings, "gateway_shared_secret", GATEWAY_SECRET)
    monkeypatch.setattr(settings, "collaboration_api_url", "")
    sc = MagicMock(name="storage_client")
    sc.list_baton_notices = AsyncMock(return_value=[_row(1)])
    monkeypatch.setattr(bn, "get_storage_client", lambda: sc)
    return sc


def _tenant_key(tenant_id: str) -> dict:
    return {"X-Gateway-Secret": GATEWAY_SECRET, "X-Tenant-ID": tenant_id}


@pytest.mark.integration
@pytest.mark.parametrize("route", ["/api/v1/search", "/api/v1/recall"])
async def test_tenant_key_with_filter_agent_id_gets_that_agents_notices(
    client, owed, route
):
    tenant_id = f"t-hb09-{uuid.uuid4().hex[:8]}"
    resp = await client.post(
        route,
        headers=_tenant_key(tenant_id),
        json={
            "tenant_id": tenant_id,
            "query": "anything",
            "filter_agent_id": "plugin-agent",
        },
    )
    assert resp.status_code == 200, resp.text
    assert [n["baton_id"] for n in resp.json()["baton_notices"]] == [
        _row(1)["baton_id"]
    ]
    owed.list_baton_notices.assert_awaited_once_with(tenant_id, "plugin-agent", limit=2)


@pytest.mark.integration
async def test_agent_key_is_the_identity_whatever_the_body_filters(client, owed):
    tenant_id = f"t-hb09-{uuid.uuid4().hex[:8]}"
    resp = await client.post(
        "/api/v1/search",
        headers={**_tenant_key(tenant_id), "X-Agent-ID": "keyed-agent"},
        json={"tenant_id": tenant_id, "query": "anything", "filter_agent_id": "author"},
    )
    assert resp.status_code == 200, resp.text
    owed.list_baton_notices.assert_awaited_once_with(tenant_id, "keyed-agent", limit=2)


@pytest.mark.integration
async def test_tenant_key_naming_no_agent_looks_nothing_up(client, owed):
    tenant_id = f"t-hb09-{uuid.uuid4().hex[:8]}"
    resp = await client.post(
        "/api/v1/search",
        headers=_tenant_key(tenant_id),
        json={"tenant_id": tenant_id, "query": "anything"},
    )
    assert resp.status_code == 200, resp.text
    assert "baton_notices" not in resp.json()
    owed.list_baton_notices.assert_not_awaited()


# ---------------------------------------------------------------------------
# End to end on the live database: the bus records a baton, the receiver
# sees it on an unrelated recall, reads it, accepts it, and it is gone
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_receiver_sees_reads_and_accepts_a_baton(
    client, tenant_id, as_agent, monkeypatch
) -> None:
    from core_api.clients.storage_client import get_storage_client

    monkeypatch.setattr(settings, "collaboration_api_url", "")
    sc = get_storage_client()
    w = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "agent_id": "sender",
            "memory_type": "fact",
            "content": "The csv-diff parser chokes on quoted newlines." + _PADDING,
            "visibility": "scope_team",
        },
        headers=get_admin_headers(),
    )
    assert w.status_code in (200, 201), w.text
    memory_id = w.json()["id"]
    baton_id = str(uuid.uuid4())
    created_at = datetime.now(UTC).isoformat()
    # What the bus writes on a baton send (enterprise ``baton.py``).
    await sc.upsert_document(
        {
            "tenant_id": tenant_id,
            "collection": "batons",
            "doc_id": baton_id,
            "agent_id": "sender",
            "data": {
                "type": "handoff_baton",
                "v": 1,
                "baton_id": baton_id,
                "goal": "Finish the csv-diff edge cases",
                "decisions": ["Use the stdlib csv module"],
                "failed_approaches": [
                    {"what": "regex split", "why": "quoted newlines"}
                ],
                "open_questions": [],
                "memory_ids": [memory_id],
                "doc_ids": [],
                "from_agent": "sender",
                "key_hash": "h",
                "created_at": created_at,
                "recipients": [
                    {
                        "agent_id": "receiver",
                        "message_id": "msg-1",
                        "memory_ids": [memory_id],
                        "links_withheld": 0,
                    }
                ],
            },
        }
    )
    assert (
        await sc.record_baton_recipients(
            tenant_id,
            baton_id,
            from_agent="sender",
            goal="Finish the csv-diff edge cases",
            created_at=created_at,
            recipients=[{"agent_id": "receiver", "message_id": "msg-1"}],
        )
        == 1
    )

    async def recall(agent: str) -> dict:
        as_agent(tenant_id, agent)
        r = await client.post(
            "/api/v1/search",
            json={"tenant_id": tenant_id, "query": "lunch menu for friday"},
        )
        assert r.status_code == 200, r.text
        await _settle()
        return r.json()

    [notice] = (await recall("receiver"))["baton_notices"]
    assert (notice["baton_id"], notice["from_agent"]) == (baton_id, "sender")
    assert "baton_notices" not in await recall("bystander")

    as_agent(tenant_id, "receiver")
    got = await client.get(f"/api/v1/batons/{baton_id}?tenant_id={tenant_id}")
    assert got.status_code == 200, got.text
    baton = got.json()
    assert baton["decisions"] == ["Use the stdlib csv module"]
    assert baton["memory_ids"] == [memory_id]
    assert baton["linked"][0]["memory_id"] == memory_id
    assert (
        "recipients" not in baton
        and "key_hash" not in baton
        and "links_withheld" not in json.dumps(baton)
    )

    as_agent(tenant_id, "bystander")
    assert (
        await client.get(f"/api/v1/batons/{baton_id}?tenant_id={tenant_id}")
    ).status_code == 404
    assert (
        await client.post(f"/api/v1/batons/{baton_id}/accept?tenant_id={tenant_id}")
    ).status_code == 404

    as_agent(tenant_id, "receiver")
    a = await client.post(f"/api/v1/batons/{baton_id}/accept?tenant_id={tenant_id}")
    assert a.status_code == 200, a.text
    assert (a.json()["state"], a.json()["agent_id"]) == ("accepted", "receiver")
    assert "baton_notices" not in await recall("receiver")

    # The plugin's shape: a tenant key names the agent; without one it is refused.
    as_agent(tenant_id, None)
    url = f"/api/v1/batons/{baton_id}/accept?tenant_id={tenant_id}"
    assert (await client.post(url, headers=get_admin_headers())).status_code == 403
    again = await client.post(f"{url}&agent_id=receiver", headers=get_admin_headers())
    assert (
        again.status_code == 200
        and again.json()["accepted_at"] == a.json()["accepted_at"]
    )
