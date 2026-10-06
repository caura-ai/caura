"""An agent's graph reads survive more than 1000 evidence memories (M-41).

``filter_relations_by_evidence_visibility`` fetched every evidence memory it had
not already shown in one ``bulk_get_memories`` call, and storage refuses more
than 1000 ids per request with a 422. So ``GET /graph`` and ``GET /entities/{id}``
answered 500 to every agent credential once a tenant's relations cited more than
1000 distinct evidence memories. The client now sends a longer list in chunks
of the storage cap and returns the rows in input order.
"""

from __future__ import annotations

import uuid

import pytest

from core_api.clients.storage_client import CoreStorageClient
from core_api.services.entity_service import filter_relations_by_evidence_visibility
from tests.conftest import get_test_auth

_STORAGE_CAP = 1000


@pytest.mark.unit
async def test_bulk_get_sends_chunks_of_the_storage_cap_in_order():
    sc = CoreStorageClient()
    sent: list[list[str]] = []

    async def fake_post(path, data=None, *, read=False, idempotent=False):
        assert path == "/memories/bulk-get"
        sent.append(data["ids"])
        return [{"id": i} for i in data["ids"]]

    sc._post = fake_post  # type: ignore[method-assign]
    ids = [str(uuid.uuid4()) for _ in range(2 * _STORAGE_CAP + 1)]

    rows = await sc.bulk_get_memories(ids, tenant_id="t")

    assert [len(chunk) for chunk in sent] == [_STORAGE_CAP, _STORAGE_CAP, 1]
    assert [row["id"] for row in rows] == ids


@pytest.mark.integration
async def test_the_evidence_filter_reads_past_the_storage_cap(client, tenant_id):
    """1,500 evidence ids: 1,499 that do not exist and one the agent wrote."""
    headers = get_test_auth(tenant_id)[1]
    resp = await client.post(
        "/api/v1/memories",
        headers=headers,
        json={
            "tenant_id": tenant_id,
            "agent_id": "evidence-bot",
            "content": f"Ada maintains the billing service {uuid.uuid4().hex}",
        },
    )
    assert resp.status_code == 201, resp.text
    relations = [{"evidence_memory_id": str(uuid.uuid4())} for _ in range(1500)]
    relations[1200] = {"evidence_memory_id": resp.json()["id"]}

    kept = await filter_relations_by_evidence_visibility(
        relations,
        tenant_id=tenant_id,
        caller_agent_id="evidence-bot",
        caller_tenant_id=tenant_id,
    )

    assert kept == [relations[1200]]
