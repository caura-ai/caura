"""A handler that fails after claiming an Idempotency-Key releases the claim.

The claim is a pending row; only ``record()`` used to clear it. Any failure
between the claim and ``record()`` — a deterministic 403/404/422, or a 429 from
the per-tenant slot taken after the claim — left the row pending, so every
retry with the same key polled for the full budget and got 409 "still in
progress" until the pending TTL lapsed, masking the real error.
"""

import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import core_api.middleware.idempotency as idem_mod
from tests.conftest import get_test_auth, uid

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _short_poll(monkeypatch):
    # The pre-fix symptom is a poll that runs out; one attempt is enough to
    # observe it without spending the full ten-second budget per retry.
    monkeypatch.setattr(idem_mod, "_CLAIM_POLL_MAX_ATTEMPTS", 1)


def _key() -> str:
    return f"release-{uuid.uuid4().hex}"


async def test_single_write_failure_releases_the_claim(client, sc, monkeypatch):
    import core_api.routes.memories as memories_route

    failing = AsyncMock(side_effect=HTTPException(status_code=422, detail="rejected"))
    monkeypatch.setattr(memories_route, "create_memory", failing)

    tenant_id, headers = get_test_auth()
    key = _key()
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"idem-release-{uid()}",
        "memory_type": "fact",
        "content": f"release content {uid()}",
    }
    h = {**headers, "Idempotency-Key": key}

    first = await client.post("/api/v1/memories", json=body, headers=h)
    assert first.status_code == 422, first.text
    assert await sc.get_idempotency(tenant_id, f"header:{key}") is None

    # The retry runs the handler again and sees the real error.
    second = await client.post("/api/v1/memories", json=body, headers=h)
    assert second.status_code == 422, second.text
    assert failing.await_count == 2

    # And once the cause is gone the same key goes through and is recorded.
    monkeypatch.undo()
    third = await client.post("/api/v1/memories", json=body, headers=h)
    assert third.status_code == 201, third.text
    replay = await client.post("/api/v1/memories", json=body, headers=h)
    assert replay.status_code == 201
    assert replay.json()["id"] == third.json()["id"]


async def test_bulk_write_failure_releases_the_claim(client, monkeypatch):
    import core_api.routes.memories as memories_route

    monkeypatch.setattr(
        memories_route,
        "_write_memories_bulk_inner",
        AsyncMock(side_effect=HTTPException(status_code=429, detail="slow down")),
    )
    tenant_id, headers = get_test_auth()
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"idem-release-bulk-{uid()}",
        "items": [{"content": f"bulk release {uid()}"}],
    }
    h = {**headers, "Idempotency-Key": _key(), "X-Bulk-Attempt-Id": f"rel-{uid()}"}

    first = await client.post("/api/v1/memories/bulk", json=body, headers=h)
    second = await client.post("/api/v1/memories/bulk", json=body, headers=h)

    assert first.status_code == 429, first.text
    assert second.status_code == 429, second.text


async def test_document_write_failure_releases_the_claim(client):
    # An invalid skills slug is rejected AFTER the claim, deterministically.
    tenant_id, headers = get_test_auth()
    body = {
        "tenant_id": tenant_id,
        "collection": "skills",
        "doc_id": "Not A Slug!",
        "data": {"summary": "x"},
    }
    h = {**headers, "Idempotency-Key": _key()}

    first = await client.post("/api/v1/documents", json=body, headers=h)
    second = await client.post("/api/v1/documents", json=body, headers=h)

    assert first.status_code == 422, first.text
    assert second.status_code == 422, second.text
    assert "still in progress" not in second.text


async def test_release_never_deletes_a_recorded_response(sc):
    tenant_id = f"release-tenant-{uid()}"
    key = f"header:{_key()}"
    await sc.upsert_idempotency(
        tenant_id=tenant_id,
        idempotency_key=key,
        request_hash="h",
        response_body={"ok": True},
        status_code=201,
        expires_at="2999-01-01T00:00:00+00:00",
    )

    released = await sc.release_idempotency_claim(
        tenant_id=tenant_id, idempotency_key=key, request_hash="h"
    )

    assert released is False
    row = await sc.get_idempotency(tenant_id, key)
    assert row is not None and row["response_body"] == {"ok": True}


async def test_cancellation_keeps_the_claim(monkeypatch):
    """A disconnect may land after the work committed; the pending claim is
    what stops an immediate retry from running it twice."""
    from core_api.middleware.idempotency import IdempotencyGuard, release_claim_on_error

    storage = AsyncMock()
    monkeypatch.setattr(idem_mod, "get_storage_client", lambda: storage)
    guard = IdempotencyGuard("t", "header:k", "h", cached=None, claimed=True)

    with pytest.raises(asyncio.CancelledError):
        async with release_claim_on_error(guard):
            raise asyncio.CancelledError

    storage.release_idempotency_claim.assert_not_called()


async def test_only_the_claiming_request_releases(monkeypatch):
    from core_api.middleware.idempotency import IdempotencyGuard, release_claim_on_error

    storage = AsyncMock()
    monkeypatch.setattr(idem_mod, "get_storage_client", lambda: storage)
    degraded = IdempotencyGuard("t", "header:k", "h", cached=None)

    with pytest.raises(RuntimeError):
        async with release_claim_on_error(degraded):
            raise RuntimeError("boom")

    storage.release_idempotency_claim.assert_not_called()


async def test_a_failed_release_does_not_mask_the_original_error(monkeypatch):
    from core_api.middleware.idempotency import IdempotencyGuard, release_claim_on_error

    storage = AsyncMock()
    storage.release_idempotency_claim.side_effect = RuntimeError("storage down")
    monkeypatch.setattr(idem_mod, "get_storage_client", lambda: storage)
    guard = IdempotencyGuard("t", "header:k", "h", cached=None, claimed=True)

    with pytest.raises(HTTPException) as exc:
        async with release_claim_on_error(guard):
            raise HTTPException(status_code=403, detail="nope")

    assert exc.value.status_code == 403
