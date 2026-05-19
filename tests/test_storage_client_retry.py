"""F5 — storage_client retries transient ConnectTimeout / 5xx on idempotent calls.

Closes the silent-extraction symptom on memclaw.dev (root cause:
``httpx.ConnectTimeout`` when ``core-api`` reaches ``storage-api``
during ``upsert_entity`` — 31% failure rate over 7 days of staging
traffic per Cloud Run logs). The outer ``except Exception`` in
``entity_extraction_worker.process_entity_extraction`` silently
absorbs the timeout, leaving ``entity_links`` empty.

Tests pinned BEFORE the implementation. They will FAIL against
current main (no retry logic exists in ``storage_client._get`` /
``_get_list`` / ``_patch`` / ``_delete``). Implementation makes
them pass.

Scope: retries are added ONLY to idempotent methods (GET, PATCH,
DELETE). POST is left alone — entity-extraction's create path goes
through POST and create-without-idempotency-key would risk double-
inserts on retry. POST retries can be added later if needed once
storage-side idempotency keys are in place.

Retry policy
────────────
- Max 3 attempts (1 initial + 2 retries)
- Retryable exceptions: ``httpx.ConnectTimeout``, ``httpx.ReadTimeout``,
  ``httpx.PoolTimeout``
- Retryable HTTP statuses: 502, 503, 504
- Exponential backoff with small jitter, capped: ~0.2s, ~0.4s
- Worst-case added latency: < 1s
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok_response(status: int = 200, body: dict | None = None) -> MagicMock:
    """Build a mock httpx.Response that behaves like a real one for our use."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.json.return_value = body if body is not None else {"id": "x", "name": "y"}
    resp.raise_for_status = MagicMock()
    if status >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "boom", request=MagicMock(), response=resp
        )
    return resp


async def _make_client():
    """Construct a CoreStorageClient with mockable async httpx clients."""
    from core_api.clients.storage_client import CoreStorageClient

    write_client = AsyncMock(spec=httpx.AsyncClient)
    read_client = AsyncMock(spec=httpx.AsyncClient)
    return (
        CoreStorageClient(
            base_url="http://test-storage",
            read_url="",
            http=write_client,
            read_http=read_client,
        ),
        write_client,
        read_client,
    )


# ---------------------------------------------------------------------------
# GET — read path retries
# ---------------------------------------------------------------------------


async def test_get_retries_on_connect_timeout_then_succeeds() -> None:
    """The exact failure mode observed on memclaw.dev: ConnectTimeout on
    one attempt, then storage-api responds normally on the retry."""
    client, _write, read = await _make_client()
    read.get = AsyncMock(
        side_effect=[
            httpx.ConnectTimeout("simulated cold start"),
            _ok_response(200, {"id": "abc"}),
        ]
    )

    result = await client._get("/entities/exact", read=True)

    assert result == {"id": "abc"}
    assert read.get.await_count == 2  # 1 failed + 1 retried = recovered


async def test_get_retries_then_gives_up_after_max_attempts() -> None:
    """When storage stays unreachable, we eventually raise the original
    timeout so the caller (the worker's except-block) still sees a
    clear failure mode in logs."""
    client, _write, read = await _make_client()
    read.get = AsyncMock(side_effect=httpx.ConnectTimeout("perma down"))

    with pytest.raises(httpx.ConnectTimeout):
        await client._get("/entities/exact", read=True)

    assert read.get.await_count == 3  # 1 initial + 2 retries = max attempts


async def test_get_retries_on_connect_error_then_succeeds() -> None:
    """ConnectError covers refused / DNS-not-yet-resolved / route-down — all
    transient during Cloud Run autoscaling and storage-api restarts.
    Local chaos test (network disconnect) raised ConnectError, not
    ConnectTimeout, when storage-api was unreachable."""
    client, _write, read = await _make_client()
    read.get = AsyncMock(
        side_effect=[
            httpx.ConnectError("Name or service not known"),
            _ok_response(200, {"id": "abc"}),
        ]
    )

    result = await client._get("/entities/exact", read=True)

    assert result == {"id": "abc"}
    assert read.get.await_count == 2


async def test_get_retries_on_5xx_status() -> None:
    """503 from storage-api (e.g. autoscaling cold-start, load-shedding)
    is also transient — same retry policy applies."""
    client, _write, read = await _make_client()
    read.get = AsyncMock(
        side_effect=[
            _ok_response(503),
            _ok_response(200, {"id": "abc"}),
        ]
    )

    result = await client._get("/entities/exact", read=True)

    assert result == {"id": "abc"}
    assert read.get.await_count == 2


async def test_get_does_not_retry_on_success_first_try() -> None:
    """No retry overhead on the happy path — the retry helper must
    short-circuit cleanly when the first attempt succeeds."""
    client, _write, read = await _make_client()
    read.get = AsyncMock(return_value=_ok_response(200, {"id": "abc"}))

    result = await client._get("/entities/exact", read=True)

    assert result == {"id": "abc"}
    assert read.get.await_count == 1


async def test_get_does_not_retry_on_404() -> None:
    """404 means "no such row" — a legitimate response, not a transient
    failure. The existing _get contract returns None on 404."""
    client, _write, read = await _make_client()
    read.get = AsyncMock(return_value=_ok_response(404))

    result = await client._get("/entities/exact", read=True)

    assert result is None
    assert read.get.await_count == 1


async def test_get_does_not_retry_on_4xx_client_error() -> None:
    """4xx (except 404) means the request is wrong — retrying won't fix
    a 400 / 422. Raise immediately so the caller sees the real shape."""
    client, _write, read = await _make_client()
    read.get = AsyncMock(return_value=_ok_response(400))

    with pytest.raises(httpx.HTTPStatusError):
        await client._get("/entities/exact", read=True)

    assert read.get.await_count == 1


# ---------------------------------------------------------------------------
# PATCH — write path retries (entity updates are idempotent)
# ---------------------------------------------------------------------------


async def test_patch_retries_on_connect_timeout() -> None:
    """Observed failure 2 of 2 in the F5 logs hit ``update_entity`` (PATCH).
    PATCH on /entities/{id} replaces fields with the given values —
    idempotent — so retry is safe."""
    client, write, _read = await _make_client()
    write.patch = AsyncMock(
        side_effect=[
            httpx.ConnectTimeout("simulated"),
            _ok_response(200, {"id": "ent-1"}),
        ]
    )

    result = await client._patch("/entities/ent-1", {"canonical_name": "globex"})

    assert result == {"id": "ent-1"}
    assert write.patch.await_count == 2


async def test_patch_does_not_retry_on_404() -> None:
    """404 PATCH = the row was deleted between read and write. Existing
    contract returns None; don't burn retries on a known-permanent state."""
    client, write, _read = await _make_client()
    write.patch = AsyncMock(return_value=_ok_response(404))

    result = await client._patch("/entities/ent-1", {"x": 1})

    assert result is None
    assert write.patch.await_count == 1


# ---------------------------------------------------------------------------
# POST is NOT retried (non-idempotent without storage-side idempotency keys)
# ---------------------------------------------------------------------------


async def test_post_does_not_retry_on_connect_timeout() -> None:
    """POST endpoints in storage_client include create operations.
    Retrying a POST that may have succeeded server-side risks duplicate
    inserts. Retry semantics for POST require an idempotency key
    upstream — defer that to a follow-up. For now: POST raises on the
    first transient failure, same as today."""
    client, write, _read = await _make_client()
    write.post = AsyncMock(side_effect=httpx.ConnectTimeout("would double-create"))

    with pytest.raises(httpx.ConnectTimeout):
        await client._post("/entities", {"canonical_name": "x"})

    assert write.post.await_count == 1
