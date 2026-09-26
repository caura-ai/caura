"""A collaboration-only HTTP connection budget to the storage writer."""

import httpx
from caura_bus_platform.settings import settings

from core_api.clients import storage_client
from core_api.clients.storage_client import CoreStorageClient


class CollaborationStorageClient(CoreStorageClient):
    @staticmethod
    def _make_pool():
        return httpx.AsyncClient(
            timeout=httpx.Timeout(20, connect=3, pool=settings.http_pool_timeout),
            limits=httpx.Limits(
                max_connections=settings.http_pool_size,
                max_keepalive_connections=min(settings.http_keepalive, settings.http_pool_size),
                keepalive_expiry=60,
            ),
            trust_env=False,
        )


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


async def close_storage_client():
    global _client
    if _client is not None:
        await _client.close()
        _client = None
        storage_client._client = None
