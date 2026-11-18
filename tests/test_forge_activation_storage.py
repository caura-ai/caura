"""The reviewed document version is checked by the storage write itself."""

from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
PREFIX = "/api/v1/storage"


async def seed(storage_http):
    body = {
        "tenant_id": f"test-tenant-review-{uuid4().hex[:8]}",
        "collection": "skills",
        "doc_id": "forge/reviewed",
        "data": {
            "status": "staged",
            "content": "Reviewed bytes",
            "content_hash": "sha256:reviewed",
        },
    }
    response = await storage_http.post(f"{PREFIX}/documents", json=body)
    assert response.status_code == 200, response.text
    return body, response.json()


async def stored(storage_http, body):
    response = await storage_http.get(
        f"{PREFIX}/documents/skills/{body['doc_id']}",
        params={"tenant_id": body["tenant_id"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


def activation(body, version):
    return {
        **deepcopy(body),
        "data": {
            **body["data"],
            "status": "active",
            "activation_approval": {
                "approved_by": "reviewer-1",
                "approved_at": "2026-11-17T08:00:00+00:00",
                "content_hash": body["data"]["content_hash"],
            },
        },
        "expected_status": ["staged"],
        "expected_content_hash": body["data"]["content_hash"],
        "expected_updated_at": version["updated_at"],
    }


@pytest.mark.parametrize("change", ["content", "metadata", "status"])
async def test_storage_refuses_reviewed_version_after_a_concurrent_write(
    storage_http, change
):
    body, version = await seed(storage_http)
    replacement = deepcopy(body)
    if change == "content":
        replacement["data"].update(content="New content", content_hash="sha256:new")
    elif change == "metadata":
        replacement["data"]["description"] = "A concurrent editor's metadata"
    else:
        replacement["data"]["status"] = "quarantined"
    response = await storage_http.post(f"{PREFIX}/documents", json=replacement)
    assert response.status_code == 200, response.text
    current = await stored(storage_http, body)
    assert current["updated_at"] != version["updated_at"]

    response = await storage_http.post(
        f"{PREFIX}/documents", json=activation(body, version)
    )
    assert response.status_code == 409, response.text
    assert (await stored(storage_http, body))["data"] == replacement["data"]


async def test_storage_checks_hash_even_when_the_row_version_matches(storage_http):
    body, version = await seed(storage_http)
    request = activation(body, version)
    request["expected_content_hash"] = "sha256:stale"
    response = await storage_http.post(f"{PREFIX}/documents", json=request)
    assert response.status_code == 409, response.text
    assert (await stored(storage_http, body))["data"] == body["data"]


async def test_storage_commits_activation_and_provenance_once(storage_http):
    body, version = await seed(storage_http)
    request = activation(body, version)
    first = await storage_http.post(f"{PREFIX}/documents", json=request)
    assert first.status_code == 200, first.text
    assert (await stored(storage_http, body))["data"] == request["data"]
    request["data"]["activation_approval"]["approved_by"] = "reviewer-2"
    second = await storage_http.post(f"{PREFIX}/documents", json=request)
    assert second.status_code == 409, second.text
    assert (await stored(storage_http, body))["data"]["activation_approval"][
        "approved_by"
    ] == "reviewer-1"


async def test_an_optional_review_predicate_never_inserts_a_missing_document(
    storage_http,
):
    body, version = await seed(storage_http)
    request = activation(body, version)
    request["doc_id"] = "forge/missing"
    response = await storage_http.post(f"{PREFIX}/documents", json=request)
    assert response.status_code == 409, response.text


async def test_the_typed_client_forwards_review_predicates(sc):
    from core_api.clients.storage_client import DocumentStatusChangedError

    body = {
        "tenant_id": f"test-tenant-review-{uuid4().hex[:8]}",
        "collection": "skills",
        "doc_id": "forge/client",
        "data": {"status": "staged", "content_hash": "sha256:current"},
    }
    await sc.upsert_document(body)
    current = await sc.get_document(
        body["tenant_id"], "skills", body["doc_id"], read=False
    )
    with pytest.raises(DocumentStatusChangedError):
        await sc.upsert_document(
            {**body, "data": {"status": "active"}},
            expected_status=["staged"],
            expected_content_hash="sha256:stale",
            expected_updated_at=current["updated_at"],
        )
