"""Tests for CAURA-600 — request-wide timeout middleware + bulk
enrichment gather cap.

Exercises the production ``RequestTimeoutMiddleware`` directly against
a minimal FastAPI app, tuned with a small timeout so tests finish fast.
"""

import asyncio

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from core_api.middleware.request_timeout import RequestTimeoutMiddleware

pytestmark = pytest.mark.asyncio


def _build_test_app(timeout_seconds: float) -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestTimeoutMiddleware, timeout_seconds=timeout_seconds)

    @app.get("/fast")
    async def fast():
        return {"ok": True}

    @app.get("/slow")
    async def slow():
        await asyncio.sleep(5)
        return {"ok": True}

    @app.get("/mcp")
    async def mcp_probe():
        await asyncio.sleep(0.3)
        return {"mcp": True}

    @app.post("/api/v1/memories/bulk")
    async def bulk_probe():
        # Sleeps longer than the test budget on purpose: the route is
        # opt-out, so the middleware must let it run to completion
        # rather than synthesising a 504. The route's own
        # ``asyncio.wait_for`` (production code) handles bulk-specific
        # deadlines; here we only verify the middleware skip.
        await asyncio.sleep(0.3)
        return {"bulk": True}

    return app


async def test_fast_request_passes_through():
    app = _build_test_app(timeout_seconds=2.0)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/fast")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


async def test_slow_request_returns_504():
    app = _build_test_app(timeout_seconds=0.1)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/slow")
    assert resp.status_code == 504
    assert resp.json() == {"detail": "request timeout"}


async def test_mcp_path_bypasses_timeout():
    app = _build_test_app(timeout_seconds=0.05)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/mcp")
    assert resp.status_code == 200
    assert resp.json() == {"mcp": True}


async def test_bulk_path_bypasses_timeout():
    """CAURA-602: ``/api/v1/memories/bulk`` opts out of the global
    request-timeout middleware so the route's own deeper budget can run.
    Cancelling here is what produced silent creates under load — the
    storage commit had landed but the response to the client was killed
    mid-flight.
    """
    app = _build_test_app(timeout_seconds=0.05)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post("/api/v1/memories/bulk")
    assert resp.status_code == 200
    assert resp.json() == {"bulk": True}


async def test_gather_cancel_preserves_completed_slots():
    """Validates the pattern used in `memory_service.create_memories_bulk`:
    completed enrichments keep their in-place writes; in-flight tasks
    are cancelled and their slots remain None."""
    results: list[str | None] = [None, None, None]

    async def fast(idx: int) -> None:
        await asyncio.sleep(0.01)
        results[idx] = "done"

    async def hang(idx: int) -> None:
        await asyncio.sleep(10)
        results[idx] = "done"  # never reached

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            asyncio.gather(fast(0), hang(1), fast(2)),
            timeout=0.2,
        )
    assert results[0] == "done"
    assert results[1] is None
    assert results[2] == "done"
