"""A collaboration-only HTTP connection budget to the storage writer."""

import httpx
from caura_bus_platform.settings import settings
from caura_bus_platform.timing import http_timing_hooks

from core_api.clients import storage_client
from core_api.clients.storage_client import CoreStorageClient


class CollaborationStorageClient(CoreStorageClient):
    async def _execute(self, do_request, *, retry, label):
        # Collaboration has end-to-end deadlines and caller idempotency. Keep
        # one cancellable RPC per attempt: inherited retries/shielding/recycling
        # can otherwise outlive an admission slot and amplify overload.
        return await do_request()

    @staticmethod
    def _make_pool(pool_size=None):
        pool_size = settings.http_pool_size if pool_size is None else pool_size
        return httpx.AsyncClient(
            timeout=httpx.Timeout(20, connect=3, pool=settings.http_pool_timeout),
            limits=httpx.Limits(
                max_connections=pool_size,
                max_keepalive_connections=min(settings.http_keepalive, pool_size),
                keepalive_expiry=60,
            ),
            trust_env=False,
            event_hooks=http_timing_hooks(),
        )


class PresenceStorageClient(CollaborationStorageClient):
    @staticmethod
    def _make_pool():
        return CollaborationStorageClient._make_pool(settings.presence_http_pool_size)


_presence_client = None
_client = None


def get_storage_client():
    global _client
    if _client is None:
        # Collaboration reads require the same writer as its transactional
        # claims and lifecycle decisions, even in a split memory deployment.
        _client = CollaborationStorageClient(read_url="")
        # Auth dependencies in this workload share the same finite budget.
        storage_client._client = _client
    return _client


def get_presence_storage_client():
    global _presence_client
    if _presence_client is None:
        _presence_client = PresenceStorageClient(read_url="")
    return _presence_client


async def close_storage_client():
    global _client, _presence_client
    if _presence_client is not None:
        await _presence_client.close()
        _presence_client = None
    if _client is not None:
        await _client.close()
        _client = None
        storage_client._client = None
