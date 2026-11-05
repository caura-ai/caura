"""``batons`` belongs to the Caura bus, for reads and writes.

A handoff baton (docs/plans/handoff-batons.md) is readable by its sender and
receivers only. The bus stores it through storage and serves it at
``GET /api/v1/bus/batons/{id}`` after checking the caller. The document routes
gate by tenant and fleet, so they refuse the collection for every credential,
as they refuse ``interview_jobs``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import core_api.services.interview_service as interview_service
from core_api import mcp_server
from core_api.services.interview_service import BATON_COLLECTION
from tests._mcp_test_helpers import parse_envelope, stub_storage_client
from tests.conftest import get_test_auth, new_tenant_id, uid


async def _store_baton(tenant_id: str) -> str:
    baton_id = str(uid())
    await interview_service.get_storage_client().upsert_document(
        {
            "tenant_id": tenant_id,
            "collection": BATON_COLLECTION,
            "doc_id": baton_id,
            "data": {"goal": "secret goal", "memory_ids": [], "recipients": []},
            "agent_id": "sender",
        }
    )
    return baton_id


async def test_rest_document_reads_of_batons_are_refused(client):
    tenant_id, headers = get_test_auth(new_tenant_id())
    doc_id = await _store_baton(tenant_id)

    reads = [
        lambda: client.get(
            f"/api/v1/documents?tenant_id={tenant_id}&collection={BATON_COLLECTION}",
            headers=headers,
        ),
        lambda: client.get(
            f"/api/v1/documents/{doc_id}?tenant_id={tenant_id}&collection={BATON_COLLECTION}",
            headers=headers,
        ),
        lambda: client.post(
            "/api/v1/documents/query",
            json={"tenant_id": tenant_id, "collection": BATON_COLLECTION},
            headers=headers,
        ),
        lambda: client.post(
            "/api/v1/documents/search",
            json={
                "tenant_id": tenant_id,
                "collection": BATON_COLLECTION,
                "query": "goal",
            },
            headers=headers,
        ),
    ]
    for read in reads:
        resp = await read()
        assert resp.status_code == 400, resp.text
        assert "secret goal" not in resp.text
        assert "Caura bus" in resp.text


async def test_rest_document_writes_and_deletes_of_batons_are_refused(client):
    tenant_id, headers = get_test_auth(new_tenant_id())
    doc_id = await _store_baton(tenant_id)

    resp = await client.post(
        "/api/v1/documents",
        json={
            "tenant_id": tenant_id,
            "collection": BATON_COLLECTION,
            "doc_id": doc_id,
            "data": {"goal": "planted"},
        },
        headers=headers,
    )
    assert resp.status_code == 400, resp.text
    resp = await client.delete(
        f"/api/v1/documents/{doc_id}?tenant_id={tenant_id}&collection={BATON_COLLECTION}",
        headers=headers,
    )
    assert resp.status_code == 400, resp.text
    stored = await interview_service.get_storage_client().get_document(
        tenant_id, BATON_COLLECTION, doc_id, read=False
    )
    assert stored["data"]["goal"] == "secret goal"


@pytest.mark.parametrize("op", ["read", "query", "search", "write", "delete"])
async def test_mcp_document_ops_on_batons_are_refused(mcp_env, monkeypatch, op):
    sc = stub_storage_client(
        monkeypatch,
        get_document={
            "collection": BATON_COLLECTION,
            "doc_id": "b",
            "data": {"goal": "x"},
        },
        query_documents=[],
        search_documents_vector=[],
        upsert_document_xmax={},
        delete_document=True,
    )
    monkeypatch.setattr(
        "common.embedding.get_embedding", AsyncMock(return_value=[0.1] * 8)
    )
    out = await mcp_server.caura_doc(
        op=op,
        collection=BATON_COLLECTION,
        doc_id="b",
        query="q",
        data={"goal": "planted"},
    )
    assert "error" in parse_envelope(out), out
    assert "Caura bus" in parse_envelope(out)["error"]["message"]
    sc.get_document.assert_not_awaited()
    sc.query_documents.assert_not_awaited()
    sc.search_documents_vector.assert_not_awaited()
    sc.upsert_document_xmax.assert_not_awaited()
    sc.delete_document.assert_not_awaited()
