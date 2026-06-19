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

Scope: idempotent methods (GET, PATCH, DELETE) retry the full
transient set. POST retries connection-phase failures ONLY
(ConnectTimeout / ConnectError / PoolTimeout — raised before any
request byte is written, so a retry cannot double-insert); it does
NOT retry ReadTimeout or 5xx, where the request may have committed
storage-side. Full POST retry semantics still require idempotency
keys storage-side.

Retry policy
────────────
- Idempotent methods: max 3 attempts (1 initial + 2 retries)
- Connection-phase (POST) failures: max ``CONNECT_PHASE_MAX_ATTEMPTS`` (5) —
  they add no server load, so retrying more rides out a cold start
- Retryable exceptions: ``httpx.ConnectTimeout``, ``httpx.ReadTimeout``,
  ``httpx.PoolTimeout`` (idempotent methods); connection-phase subset
  for POST
- Retryable HTTP statuses: 502, 503, 504 (idempotent methods only)
- Exponential backoff with small jitter, capped at ``RETRY_BACKOFF_MAX_S``
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
    clear failure mode in logs. A perma ConnectTimeout is a connection-phase
    failure, so an idempotent GET rides the higher CONNECT_PHASE_MAX_ATTEMPTS
    (5) budget — same as POST — to ride out a storage cold start. ReadTimeout
    / 5xx stay capped at the default 3 (see
    test_get_logs_giving_up_when_5xx_exhausts_retries)."""
    from common.http_retry import CONNECT_PHASE_MAX_ATTEMPTS

    client, _write, read = await _make_client()
    read.get = AsyncMock(side_effect=httpx.ConnectTimeout("perma down"))

    with pytest.raises(httpx.ConnectTimeout):
        await client._get("/entities/exact", read=True)

    assert read.get.await_count == CONNECT_PHASE_MAX_ATTEMPTS == 5


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


async def test_get_logs_giving_up_when_5xx_exhausts_retries(caplog) -> None:
    """All attempts returning a retryable status must emit the same
    "giving up" signal as the exception-exhaustion path — a 3x-502
    incident should read as exhausted retries in the logs, not just a
    bare HTTPStatusError from the caller."""
    import logging

    client, _write, read = await _make_client()
    read.get = AsyncMock(return_value=_ok_response(502))

    with caplog.at_level(logging.WARNING, logger="common.http_retry"):
        with pytest.raises(httpx.HTTPStatusError):
            await client._get("/entities/exact", read=True)

    assert read.get.await_count == 3
    assert any("giving up" in r.message for r in caplog.records), caplog.records


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
# POST retries connection-phase failures ONLY (request provably never sent)
# ---------------------------------------------------------------------------
#
# Prod 2026-06-11: contradiction detection (find_similar_candidates)
# failed 42× and the audit flusher (create_audit_logs_bulk) dropped
# events — every one a first-attempt ConnectTimeout behind the VPC
# connector. ConnectTimeout / ConnectError / PoolTimeout are all raised
# before a single request byte is written, so retrying them cannot
# double-insert. ReadTimeout and 5xx stay non-retried for POST: the
# request reached storage and may have committed.


async def test_post_retries_on_connect_timeout_then_succeeds() -> None:
    """The exact prod failure mode: ConnectTimeout on the first attempt
    (cold connection through the VPC connector), success on retry."""
    client, write, _read = await _make_client()
    write.post = AsyncMock(
        side_effect=[
            httpx.ConnectTimeout("simulated cold connection"),
            _ok_response(200, {"inserted": 1}),
        ]
    )

    result = await client._post("/audit-logs/bulk", {"events": [{"a": 1}]})

    assert result == {"inserted": 1}
    assert write.post.await_count == 2


async def test_post_retries_on_connect_error_then_succeeds() -> None:
    client, write, _read = await _make_client()
    write.post = AsyncMock(
        side_effect=[
            httpx.ConnectError("Name or service not known"),
            _ok_response(200, {"id": "abc"}),
        ]
    )

    result = await client._post("/entities", {"canonical_name": "x"})

    assert result == {"id": "abc"}
    assert write.post.await_count == 2


async def test_post_gives_up_after_max_attempts_on_connect_timeout() -> None:
    """Connection-phase failures retry CONNECT_PHASE_MAX_ATTEMPTS (5) times —
    more than the ReadTimeout/5xx default (3, see
    test_get_logs_giving_up_when_5xx_exhausts_retries) — because a connect
    failure was provably never sent: it adds no load to a healthy server and
    just rides out a cold start. Idempotent GETs share this connect-phase
    budget too (see test_get_retries_then_gives_up_after_max_attempts)."""
    from common.http_retry import CONNECT_PHASE_MAX_ATTEMPTS

    client, write, _read = await _make_client()
    write.post = AsyncMock(side_effect=httpx.ConnectTimeout("storage unreachable"))

    with pytest.raises(httpx.ConnectTimeout):
        await client._post("/entities", {"canonical_name": "x"})

    assert write.post.await_count == CONNECT_PHASE_MAX_ATTEMPTS == 5


async def test_post_does_not_retry_on_read_timeout() -> None:
    """ReadTimeout means the request was sent and the response never
    arrived — storage may have committed the insert. Retrying would
    risk a double-insert, so POST must raise immediately."""
    client, write, _read = await _make_client()
    write.post = AsyncMock(side_effect=httpx.ReadTimeout("response never arrived"))

    with pytest.raises(httpx.ReadTimeout):
        await client._post("/entities", {"canonical_name": "x"})

    assert write.post.await_count == 1


async def test_post_does_not_retry_on_5xx() -> None:
    """A 5xx response proves the request reached storage — it may have
    partially committed before failing. No retry for POST."""
    client, write, _read = await _make_client()
    write.post = AsyncMock(return_value=_ok_response(503))

    with pytest.raises(httpx.HTTPStatusError):
        await client._post("/entities", {"canonical_name": "x"})

    assert write.post.await_count == 1


async def test_post_optional_retries_on_connect_timeout_then_succeeds() -> None:
    client, write, _read = await _make_client()
    write.post = AsyncMock(
        side_effect=[
            httpx.ConnectTimeout("simulated"),
            _ok_response(200, {"ok": True}),
        ]
    )

    result = await client._post_optional("/verification-codes/c1/use")

    assert result == {"ok": True}
    assert write.post.await_count == 2


async def test_backoff_delay_cap_is_a_hard_ceiling() -> None:
    """``RETRY_BACKOFF_MAX_S`` is a HARD ceiling — jitter is applied before the
    cap, so no single backoff sleep exceeds it even at the highest attempt.
    (Regression: capping before jitter let the real ceiling reach MAX * 1.1.)"""
    from common.http_retry import RETRY_BACKOFF_MAX_S, _backoff_delay

    # Attempts 5+ saturate the cap; sample widely to catch the jittered maximum.
    for attempt in range(1, 9):
        for _ in range(200):
            assert 0.0 <= _backoff_delay(attempt) <= RETRY_BACKOFF_MAX_S


# ---------------------------------------------------------------------------
# Audit bulk flush — idempotent POST retries the FULL transient set
# ---------------------------------------------------------------------------
#
# Each event carries a ``client_event_id`` and storage dedups on it under the
# per-tenant chain-head lock, so a retry of a lost-ack batch can't double-append
# to the tamper-evident hash chain. That makes ReadTimeout / 5xx safe to retry
# for create_audit_logs_bulk (idempotent=True) — unlike a bare POST, which must
# raise on those because the request may have committed (the POST tests above).
# Recovers the silent audit-slice drop from the connect-phase-only path.

_AUDIT_EVENT = {
    "tenant_id": "t",
    "action": "a",
    "resource_type": "r",
    "client_event_id": "ev-1",
}


async def test_audit_bulk_retries_on_read_timeout_then_succeeds() -> None:
    client, write, _read = await _make_client()
    write.post = AsyncMock(
        side_effect=[
            httpx.ReadTimeout("response never arrived"),
            _ok_response(200, {"ok": True, "count": 1}),
        ]
    )

    result = await client.create_audit_logs_bulk([dict(_AUDIT_EVENT)])

    assert result == {"ok": True, "count": 1}
    assert write.post.await_count == 2


async def test_audit_bulk_retries_on_5xx_then_succeeds() -> None:
    client, write, _read = await _make_client()
    write.post = AsyncMock(
        side_effect=[
            _ok_response(503),
            _ok_response(200, {"ok": True, "count": 1}),
        ]
    )

    result = await client.create_audit_logs_bulk([dict(_AUDIT_EVENT)])

    assert result == {"ok": True, "count": 1}
    assert write.post.await_count == 2
