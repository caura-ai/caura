"""Incident 2026-06-16 — storage_client connection-pool slot leak + recovery.

Root cause: ``CoreStorageClient`` holds a process-wide singleton httpx pool
(``max_connections=200``, ``pool=5s``). When an in-flight storage call was
cancelled — the 35s enrichment ``wait_for`` or the 45s request-timeout firing
mid-request — httpcore (1.0.9) did not return the connection to the pool.
Leaked slots accumulated over ~4.5 days of uptime until every acquire hit
``PoolTimeout``; the singleton never self-healed, so recovery was restart-only
(a ~2h SEV-2 on eToro on-prem).

Two fixes, one regression test each:

* Fix B (prevention) — ``_cancel_safe`` shields the in-flight request so a
  cancelled caller still lets the request finish and release its slot.
* Fix A (recovery) — ``_execute`` recycles the pool once on ``PoolTimeout``
  exhaustion so a leaked-out client self-heals in seconds.

Plus a guard test: a transient ``PoolTimeout`` that the retry policy rides out
must NOT trigger a recycle (don't churn the pool on a single blip).

Tests mirror ``test_storage_client_retry.py``'s AsyncMock-client convention.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

pytestmark = pytest.mark.asyncio


def _ok_response(status: int = 200, body: dict | None = None) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.json.return_value = body if body is not None else {"id": "x"}
    resp.raise_for_status = MagicMock()
    if status >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "boom", request=MagicMock(), response=resp
        )
    return resp


async def _make_client():
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
# Fix B — cancellation must not strand the in-flight request (slot leak)
# ---------------------------------------------------------------------------


async def test_cancellation_lets_inflight_request_finish_and_release() -> None:
    """The leak's trigger: the caller is cancelled mid-request (35s/45s budget).

    With the shield, the underlying storage call still runs to completion —
    so httpx returns its pooled connection — even though the caller observes
    ``CancelledError`` immediately. Without it, cancelling the caller cancels
    the request mid-flight and the connection is the one that leaks.
    """
    client, _write, read = await _make_client()

    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def slow_get(*_args, **_kwargs):
        started.set()
        await release.wait()  # request "in flight" against the pool
        finished.set()
        return _ok_response(200, {"id": "ok"})

    read.get = slow_get

    task = asyncio.create_task(client._get("/slow", read=True))
    await asyncio.wait_for(started.wait(), timeout=1.0)

    task.cancel()  # mimic the enrichment/request budget cancelling the caller
    with pytest.raises(asyncio.CancelledError):
        await task

    # Fix B: the shielded request is allowed to complete → slot returned.
    release.set()
    await asyncio.wait_for(finished.wait(), timeout=1.0)
    assert finished.is_set()


# ---------------------------------------------------------------------------
# Fix A — pool self-heals on exhaustion (was restart-only)
# ---------------------------------------------------------------------------


async def test_post_recycles_pool_on_exhaustion_then_succeeds(monkeypatch) -> None:
    """A POST whose pool is exhausted (``PoolTimeout`` survives the connect-phase
    retries) recycles the pool once and succeeds on the fresh one — converting a
    restart-only outage into a seconds-long self-heal."""
    client, write, _read = await _make_client()
    write.post = AsyncMock(side_effect=httpx.PoolTimeout("pool exhausted"))

    healed = AsyncMock(spec=httpx.AsyncClient)
    healed.post = AsyncMock(return_value=_ok_response(200, {"id": "healed"}))
    monkeypatch.setattr(client, "_make_pool", lambda: healed)

    result = await client._post("/entities", {"canonical_name": "x"})

    assert result == {"id": "healed"}
    assert client._pool_generation == 1  # exactly one rebuild
    # Exhausted on every connect-phase attempt, then one success on the new pool.
    from common.http_retry import CONNECT_PHASE_MAX_ATTEMPTS

    assert write.post.await_count == CONNECT_PHASE_MAX_ATTEMPTS
    assert healed.post.await_count == 1
    # The old, exhausted pool is force-closed in the background.
    await asyncio.sleep(0)
    write.aclose.assert_awaited()


async def test_get_recycles_pool_on_exhaustion_then_succeeds(monkeypatch) -> None:
    """Read path heals too: the suppression GET on every authed request was the
    most-visible victim, so its recovery is what restores service."""
    client, _write, read = await _make_client()
    read.get = AsyncMock(side_effect=httpx.PoolTimeout("pool exhausted"))

    healed = AsyncMock(spec=httpx.AsyncClient)
    healed.get = AsyncMock(return_value=_ok_response(200, {"id": "healed"}))
    monkeypatch.setattr(client, "_make_pool", lambda: healed)

    result = await client._get("/tenant-suppression/t1", read=True)

    assert result == {"id": "healed"}
    assert client._pool_generation == 1
    assert healed.get.await_count == 1


async def test_transient_pooltimeout_does_not_recycle(monkeypatch) -> None:
    """Guard: a single ``PoolTimeout`` that the retry policy rides out must NOT
    recycle the pool — recycling is reserved for genuine exhaustion, not a blip."""
    client, _write, read = await _make_client()
    read.get = AsyncMock(
        side_effect=[httpx.PoolTimeout("blip"), _ok_response(200, {"id": "abc"})]
    )
    rebuilt = MagicMock(return_value=AsyncMock(spec=httpx.AsyncClient))
    monkeypatch.setattr(client, "_make_pool", rebuilt)

    result = await client._get("/entities/exact", read=True)

    assert result == {"id": "abc"}
    assert client._pool_generation == 0  # no recycle
    rebuilt.assert_not_called()
    assert read.get.await_count == 2  # recovered within the normal retry budget
