"""CP-13 — correction notices on recall, the core-api half.

An agent the bus cannot tell about a correction hears about it on its next
recall: ``correction_notices`` on MCP ``caura_recall`` and REST ``/search`` and
``/recall``. At most three, compact, absent when there are none (no new bytes
on the ordinary recall), never for a caller with no agent identity or a person,
and a failed lookup reads as none. Storage's half (who is owed a notice) is
``core-storage-api/tests/test_correction_notices.py``.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core_api import mcp_server
from core_api.audit_actor import audit_principal
from core_api.config import settings
from core_api.schemas import CorrectionNotice, SearchResponse
from core_api.services import correction_notices as cn
from tests._legacy_contracts import LEGACY_API_KEY_FIELD
from tests._mcp_test_helpers import parse_envelope, stub_storage_client


def _row(i: int) -> dict:
    return {
        "correction_id": f"c-{i}",
        "memory_id": f"m-{i}",
        "old_summary": "deploys on Tuesday",
        "new_summary": "deploys on Thursday",
        "corrected_at": f"2026-11-0{i}T09:00:00+00:00",
    }


@pytest.fixture
def storage(monkeypatch):
    sc = MagicMock(name="storage_client")
    sc.list_correction_notices = AsyncMock(return_value=[])
    monkeypatch.setattr(cn, "get_storage_client", lambda: sc)
    return sc


# ---------------------------------------------------------------------------
# The service: who is looked up, and what a notice looks like
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_notices_are_compact_capped_and_carry_how_to_confirm(storage):
    storage.list_correction_notices.return_value = [_row(i) for i in (1, 2, 3, 4)]

    out = await cn.correction_notices("t-1", "agent-a", surface="mcp")

    storage.list_correction_notices.assert_awaited_once_with("t-1", "agent-a", limit=3)
    assert [n["correction_id"] for n in out] == ["c-1", "c-2", "c-3"]
    assert set(out[0]) == {
        "correction_id",
        "memory_id",
        "old_summary",
        "new_summary",
        "corrected_at",
        "ask",
    }
    assert "op=confirm_correction" in out[0]["ask"]
    rest = await cn.correction_notices("t-1", "agent-a", surface="rest")
    assert "/api/v1/corrections/c-1/confirm" in rest[0]["ask"]


@pytest.mark.unit
async def test_no_agent_identity_or_a_person_looks_nothing_up(storage):
    assert await cn.correction_notices("t-1", None, surface="rest") == []
    assert cn.start_notices("t-1", None, surface="rest") is None
    token = audit_principal.set({"credential_kind": "person"})
    try:
        assert await cn.correction_notices("t-1", "agent-a", surface="rest") == []
        assert cn.start_notices("t-1", "agent-a", surface="rest") is None
    finally:
        audit_principal.reset(token)
    storage.list_correction_notices.assert_not_awaited()


@pytest.mark.unit
async def test_a_failed_lookup_is_no_notices(storage):
    storage.list_correction_notices.side_effect = RuntimeError("storage down")
    task = cn.start_notices("t-1", "agent-a", surface="rest")
    assert await cn.finish_notices(task) == []


# ---------------------------------------------------------------------------
# REST envelope: absent when empty, byte for byte
# ---------------------------------------------------------------------------


def _serve(response: SearchResponse) -> bytes:
    """``response`` through FastAPI's own ``response_model`` serialization, as /search sends it."""
    app = FastAPI()

    @app.get("/s", response_model=SearchResponse)
    async def _s():
        return response

    return TestClient(app).get("/s").content


@pytest.mark.unit
def test_search_payload_unchanged_without_notices():
    plain = _serve(SearchResponse(items=[], recall_tracked=False))
    assert b"correction_notices" not in plain
    assert (
        _serve(SearchResponse(items=[], recall_tracked=False, correction_notices=None))
        == plain
    )
    assert (
        _serve(SearchResponse(items=[], recall_tracked=False, correction_notices=[]))
        == plain
    )


@pytest.mark.unit
def test_search_payload_carries_notices_when_owed():
    notice = CorrectionNotice(**_row(1), ask="confirm")
    body = json.loads(_serve(SearchResponse(items=[], correction_notices=[notice])))
    assert body["correction_notices"] == [notice.model_dump()]


# ---------------------------------------------------------------------------
# MCP caura_recall
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
async def test_mcp_recall_carries_notices_on_any_query(mcp_env, monkeypatch):
    mcp_env["service"]("search_memories").return_value = [_MemoryStub()]
    monkeypatch.setattr(mcp_server, "resolve_config", _fake_resolve_config)
    sc = stub_storage_client(
        monkeypatch, get_agent=None, list_correction_notices=[_row(1)]
    )

    payload = parse_envelope(await mcp_server.caura_recall(query="something unrelated"))

    assert payload["count"] == 1
    (notice,) = payload["correction_notices"]
    assert notice["correction_id"] == "c-1"
    assert "op=confirm_correction" in notice["ask"]
    assert sc.list_correction_notices.await_args.kwargs == {"limit": 3}


@pytest.mark.unit
async def test_mcp_recall_omits_notices_when_none(mcp_env, monkeypatch):
    mcp_env["service"]("search_memories").return_value = [_MemoryStub()]
    monkeypatch.setattr(mcp_server, "resolve_config", _fake_resolve_config)
    stub_storage_client(monkeypatch, get_agent=None)

    payload = parse_envelope(await mcp_server.caura_recall(query="anything"))
    assert "correction_notices" not in payload


# ---------------------------------------------------------------------------
# REST, whose agent: the OpenClaw plugin searches on the TENANT key
# ---------------------------------------------------------------------------
#
# The plugin's automatic /search names its agent in the body, not with an agent
# key. The notices follow the same identity the search runs as and exposure
# records: an agent key, else caller_agent_id, else filter_agent_id.


GATEWAY_SECRET = "test-gateway-secret"


@pytest.fixture
def owed(monkeypatch):
    # The gateway-header path, as on SaaS: the tenant key arrives as X-Tenant-ID
    # and an agent key adds X-Agent-ID.
    monkeypatch.setattr(settings, "is_standalone", False)
    monkeypatch.setattr(settings, LEGACY_API_KEY_FIELD, "")
    monkeypatch.setattr(settings, "gateway_shared_secret", GATEWAY_SECRET)
    sc = MagicMock(name="storage_client")
    sc.list_correction_notices = AsyncMock(return_value=[_row(1)])
    monkeypatch.setattr(cn, "get_storage_client", lambda: sc)
    return sc


def _tenant_key(tenant_id: str) -> dict:
    # The gateway's tenant-key path: X-Tenant-ID, no X-Agent-ID.
    return {"X-Gateway-Secret": GATEWAY_SECRET, "X-Tenant-ID": tenant_id}


@pytest.mark.integration
@pytest.mark.parametrize("route", ["/api/v1/search", "/api/v1/recall"])
async def test_tenant_key_with_filter_agent_id_gets_that_agents_notices(
    client, owed, route
):
    tenant_id = f"t-cp13-{uuid.uuid4().hex[:8]}"
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
    assert [n["correction_id"] for n in resp.json()["correction_notices"]] == ["c-1"]
    owed.list_correction_notices.assert_awaited_once_with(
        tenant_id, "plugin-agent", limit=3
    )


@pytest.mark.integration
async def test_tenant_key_caller_agent_id_wins_over_filter(client, owed):
    tenant_id = f"t-cp13-{uuid.uuid4().hex[:8]}"
    resp = await client.post(
        "/api/v1/search",
        headers=_tenant_key(tenant_id),
        json={
            "tenant_id": tenant_id,
            "query": "anything",
            "caller_agent_id": "reader",
            "filter_agent_id": "author",
        },
    )
    assert resp.status_code == 200, resp.text
    owed.list_correction_notices.assert_awaited_once_with(tenant_id, "reader", limit=3)


@pytest.mark.integration
async def test_agent_key_is_the_identity_whatever_the_body_filters(client, owed):
    tenant_id = f"t-cp13-{uuid.uuid4().hex[:8]}"
    resp = await client.post(
        "/api/v1/search",
        headers={**_tenant_key(tenant_id), "X-Agent-ID": "keyed-agent"},
        json={"tenant_id": tenant_id, "query": "anything", "filter_agent_id": "author"},
    )
    assert resp.status_code == 200, resp.text
    owed.list_correction_notices.assert_awaited_once_with(
        tenant_id, "keyed-agent", limit=3
    )


@pytest.mark.integration
async def test_tenant_key_naming_no_agent_looks_nothing_up(client, owed):
    tenant_id = f"t-cp13-{uuid.uuid4().hex[:8]}"
    resp = await client.post(
        "/api/v1/search",
        headers=_tenant_key(tenant_id),
        json={"tenant_id": tenant_id, "query": "anything"},
    )
    assert resp.status_code == 200, resp.text
    assert "correction_notices" not in resp.json()
    owed.list_correction_notices.assert_not_awaited()
