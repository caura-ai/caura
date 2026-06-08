import asyncio
import logging
from collections.abc import Awaitable

from fastapi import APIRouter

from core_api.clients.storage_client import get_storage_client
from core_api.constants import PROBE_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)
router = APIRouter(tags=["System"])


async def _bounded_count(label: str, coro: Awaitable[int]) -> int:
    """Run a count coroutine under ``PROBE_TIMEOUT_SECONDS`` and return
    ``0`` on timeout or any failure.

    Public ``/api/v1/stats`` is unauthenticated and backs the
    landing-page tiles, so a stalled storage backend or pool exhaustion
    must not hang every page-load. Mirrors the ``/health`` probe's
    timeout posture (same constant) so a single misbehaving query is
    bounded the same way across both public surfaces. We swallow rather
    than propagate because ``0`` is a fine "best we can do right now"
    placeholder for a tile and the caller has no way to retry usefully.
    """
    try:
        return await asyncio.wait_for(coro, timeout=PROBE_TIMEOUT_SECONDS)
    except Exception:
        # ``TimeoutError`` is a subclass of ``Exception`` so a single
        # branch covers both the wait_for timeout and any storage-side
        # failure (httpx error, JSON decode, etc.). ``exc_info=True``
        # keeps the traceback for ops without surfacing it to the
        # public response.
        logger.warning("public_stats: %s count failed; returning 0", label, exc_info=True)
        return 0


@router.get("/stats")
async def public_stats() -> dict[str, int]:
    """Minimal public counters for the landing page status bar."""
    sc = get_storage_client()
    # Run all three count queries concurrently — each is one round-trip
    # to core-storage. Each call is bounded by ``_bounded_count`` so a
    # single stalled query can't hang the whole endpoint.
    tenant_count, memory_count, agent_count = await asyncio.gather(
        _bounded_count("tenant", sc.count_distinct_tenants()),
        _bounded_count("memory", sc.count_all(tenant_id="")),
        _bounded_count("agent", sc.count_distinct_agents()),
    )
    return {
        "tenant_count": tenant_count,
        "memory_count": memory_count,
        "agent_count": agent_count,
    }
