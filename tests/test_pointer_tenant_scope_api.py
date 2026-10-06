"""Caller-supplied pointers must name rows of the caller's tenant: 422 otherwise.

``subject_entity_id`` (POST /memories, POST /memories/bulk, PATCH
/memories/{id}) and ``evidence_memory_id`` (POST /relations/upsert) come
straight from public request bodies. Storage now refuses a value that is not a
row of the write's tenant — unknown and foreign alike — with a marked 422
(core-storage-api/tests/test_pointer_tenant_scope.py). Before, an unknown id
was an FK violation that reached the caller as a retryable 5xx, and a foreign
id persisted a cross-tenant edge. These tests pin the core-api half: the
refusal reaches the caller as a 422 naming the field, not as a 500.
"""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import get_test_auth
from tests.conftest import uid as _uid

pytestmark = [pytest.mark.integration]


async def _entity(client, tenant_id: str, headers: dict) -> str:
    resp = await client.post(
        "/api/v1/entities/upsert",
        json={
            "tenant_id": tenant_id,
            "entity_type": "person",
            "canonical_name": f"Ptr {_uid()}",
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _memory_body(tenant_id: str, **extra) -> dict:
    tag = _uid()
    return {
        "tenant_id": tenant_id,
        "content": f"pointer scope fixture [{tag}]",
        "agent_id": f"ptr-agent-{tag}",
        "memory_type": "fact",
        **extra,
    }


def _assert_pointer_422(resp, field: str) -> None:
    assert resp.status_code == 422, resp.text
    assert field in resp.json()["detail"], resp.text


@pytest.mark.parametrize("which", ["foreign", "unknown"])
async def test_create_with_a_subject_outside_the_tenant_is_422(
    client, tenant_id, which
):
    _, headers = get_test_auth(tenant_id)
    other = f"test-tenant-{uuid.uuid4().hex[:8]}"
    target = (
        await _entity(client, other, headers)
        if which == "foreign"
        else str(uuid.uuid4())
    )

    resp = await client.post(
        "/api/v1/memories",
        json=_memory_body(tenant_id, subject_entity_id=target),
        headers=headers,
    )
    _assert_pointer_422(resp, "subject_entity_id")


async def test_patch_with_a_foreign_subject_is_422(client, tenant_id):
    _, headers = get_test_auth(tenant_id)
    other = f"test-tenant-{uuid.uuid4().hex[:8]}"
    foreign = await _entity(client, other, headers)
    created = await client.post(
        "/api/v1/memories", json=_memory_body(tenant_id), headers=headers
    )
    assert created.status_code == 201, created.text

    resp = await client.patch(
        f"/api/v1/memories/{created.json()['id']}?tenant_id={tenant_id}",
        json={"subject_entity_id": foreign},
        headers=headers,
    )
    _assert_pointer_422(resp, "subject_entity_id")

    own = await _entity(client, tenant_id, headers)
    ok = await client.patch(
        f"/api/v1/memories/{created.json()['id']}?tenant_id={tenant_id}",
        json={"subject_entity_id": own},
        headers=headers,
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["subject_entity_id"] == own


async def test_relation_with_foreign_evidence_is_422(client, tenant_id):
    _, headers = get_test_auth(tenant_id)
    other = f"test-tenant-{uuid.uuid4().hex[:8]}"
    foreign_mem = await client.post(
        "/api/v1/memories", json=_memory_body(other), headers=headers
    )
    assert foreign_mem.status_code == 201, foreign_mem.text
    a, b = (
        await _entity(client, tenant_id, headers),
        await _entity(client, tenant_id, headers),
    )

    resp = await client.post(
        "/api/v1/relations/upsert",
        json={
            "tenant_id": tenant_id,
            "from_entity_id": a,
            "to_entity_id": b,
            "relation_type": "knows",
            "evidence_memory_id": foreign_mem.json()["id"],
        },
        headers=headers,
    )
    _assert_pointer_422(resp, "evidence_memory_id")


@pytest.mark.parametrize("endpoint", ["from_entity_id", "to_entity_id"])
@pytest.mark.parametrize("which", ["foreign", "unknown"])
async def test_relation_with_an_endpoint_outside_the_tenant_is_422(
    client, tenant_id, endpoint, which
):
    _, headers = get_test_auth(tenant_id)
    own = await _entity(client, tenant_id, headers)
    other = f"test-tenant-{uuid.uuid4().hex[:8]}"
    target = (
        await _entity(client, other, headers)
        if which == "foreign"
        else str(uuid.uuid4())
    )
    body = {
        "tenant_id": tenant_id,
        "from_entity_id": own,
        "to_entity_id": own,
        "relation_type": "knows",
        endpoint: target,
    }
    resp = await client.post("/api/v1/relations/upsert", json=body, headers=headers)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == (
        "from_entity_id or to_entity_id does not exist in this tenant"
    )

    # A valid pair still succeeds, including an idempotent second upsert.
    body[endpoint] = await _entity(client, tenant_id, headers)
    created = await client.post("/api/v1/relations/upsert", json=body, headers=headers)
    assert created.status_code == 200, created.text
    repeated = await client.post("/api/v1/relations/upsert", json=body, headers=headers)
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["id"] == created.json()["id"]


async def test_bulk_with_a_foreign_subject_is_422(client, tenant_id):
    _, headers = get_test_auth(tenant_id)
    other = f"test-tenant-{uuid.uuid4().hex[:8]}"
    foreign = await _entity(client, other, headers)
    own = await _entity(client, tenant_id, headers)
    agent = f"ptr-bulk-{_uid()}"
    items = [
        {"content": f"pointer bulk fixture [{_uid()}]", "subject_entity_id": subject}
        for subject in (own, foreign)
    ]
    resp = await client.post(
        "/api/v1/memories/bulk",
        json={"tenant_id": tenant_id, "agent_id": agent, "items": items},
        headers={**headers, "X-Bulk-Attempt-Id": f"ptr-{_uid()}"},
    )
    _assert_pointer_422(resp, "subject_entity_id")
