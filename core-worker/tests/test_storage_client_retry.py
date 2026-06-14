"""Connection-phase retry in ``_signed_call`` (port of caura-memclaw#333).

Prod 2026-06-11: core-api's unretried storage POSTs died on
first-attempt ``httpx.ConnectTimeout`` behind the VPC connector
(contradiction detection 42x, audit events dropped). The worker's
storage client had the same exposure on its four POSTed SQL
primitives — and worse, each connect failure burns a Pub/Sub delivery
attempt against the DLQ budget for a request that never reached
storage.

Policy under test (see ``common/http_retry.py``): every request
through ``_signed_call`` retries ConnectTimeout / ConnectError /
PoolTimeout up to 3 attempts. ReadTimeout and 5xx are NOT retried
in-process — the worker's designed retry path for sent-but-failed
requests is raise → nack → Pub/Sub redelivery.

Mirrors ``tests/test_storage_client_retry.py`` (core-api) and the
harness conventions of ``test_storage_client_auth.py`` — retry is
exercised indirectly via the public endpoint helpers so a future
helper can't drift around the ``_signed_call`` choke point.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest

from core_worker.clients import identity_token, storage_client

pytestmark = pytest.mark.asyncio


def _reset_module_state() -> None:
    storage_client._client = None
    storage_client._audience = None
    identity_token._cache.clear()
    identity_token._failure_cache.clear()
    identity_token._audience_locks.clear()


@pytest.fixture(autouse=True)
def _clean_state():
    _reset_module_state()
    yield
    _reset_module_state()


def _ok_response(body: object) -> MagicMock:
    resp = MagicMock(status_code=200)
    resp.raise_for_status = MagicMock(return_value=None)
    resp.json = MagicMock(return_value=body)
    return resp


def _server_error_response(status: int = 500) -> MagicMock:
    resp = MagicMock(status_code=status)
    resp.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("boom", request=MagicMock(), response=resp)
    )
    return resp


# ── POST — the four lifecycle/suppression SQL primitives ─────────────


async def test_post_retries_on_connect_timeout_then_succeeds() -> None:
    """The exact prod failure mode: ConnectTimeout on the first
    attempt (cold connection through the VPC connector), success on
    the in-process retry — no Pub/Sub delivery attempt burned."""
    client = MagicMock(spec=httpx.AsyncClient)
    client.post = AsyncMock(
        side_effect=[
            httpx.ConnectTimeout("simulated cold connection"),
            _ok_response({"count": 3}),
        ]
    )

    count = await storage_client.archive_expired(client, tenant_id="t1", fleet_id=None)

    assert count == 3
    assert client.post.await_count == 2


async def test_post_gives_up_after_max_attempts_on_connect_timeout() -> None:
    client = MagicMock(spec=httpx.AsyncClient)
    client.post = AsyncMock(side_effect=httpx.ConnectTimeout("storage unreachable"))

    with pytest.raises(httpx.ConnectTimeout):
        await storage_client.archive_stale(client, tenant_id="t1", fleet_id=None)

    assert client.post.await_count == 3


async def test_post_does_not_retry_on_read_timeout() -> None:
    """ReadTimeout means the request was sent — the SQL primitive may
    have committed. Propagate so the consumer nacks and Pub/Sub
    redelivers (the worker's designed retry path)."""
    client = MagicMock(spec=httpx.AsyncClient)
    client.post = AsyncMock(side_effect=httpx.ReadTimeout("response never arrived"))

    with pytest.raises(httpx.ReadTimeout):
        await storage_client.upsert_tenant_suppression(
            client, tenant_id="t1", action="suppress", updated_by=None
        )

    assert client.post.await_count == 1


async def test_post_5xx_not_retried_in_process() -> None:
    """A 5xx reached storage; ``raise_for_status`` propagates so the
    consumer nacks → Pub/Sub redelivers. No in-process double-layer."""
    client = MagicMock(spec=httpx.AsyncClient)
    client.post = AsyncMock(return_value=_server_error_response())

    with pytest.raises(httpx.HTTPStatusError):
        await storage_client.purge_soft_deleted(client, tenant_id="t1", fleet_id=None, retention_days=30)

    assert client.post.await_count == 1


# ── GET / PATCH ride the same choke point ────────────────────────────


async def test_get_retries_on_connect_timeout_then_succeeds() -> None:
    """Without the retry, a connect blip turns a cache HIT into a miss
    and the worker pays a full provider embed call."""
    client = MagicMock(spec=httpx.AsyncClient)
    client.get = AsyncMock(
        side_effect=[
            httpx.ConnectTimeout("simulated"),
            _ok_response([0.1, 0.2]),
        ]
    )

    embedding = await storage_client.find_embedding_by_content_hash(
        client, tenant_id="t1", content_hash="abc"
    )

    assert embedding == [0.1, 0.2]
    assert client.get.await_count == 2


async def test_patch_retries_on_connect_error_then_succeeds() -> None:
    client = MagicMock(spec=httpx.AsyncClient)
    client.patch = AsyncMock(
        side_effect=[
            httpx.ConnectError("Name or service not known"),
            _ok_response({}),
        ]
    )

    await storage_client.update_memory_embedding(
        client,
        memory_id=uuid4(),
        tenant_id="t1",
        embedding=[0.1],
    )

    assert client.patch.await_count == 2
