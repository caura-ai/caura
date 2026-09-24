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
    # Was ``{"detail": "request timeout"}`` — a shape nothing else on the
    # surface uses, so a client parsing ``error.code`` got nothing. See
    # ``test_the_504_carries_the_canonical_envelope`` below for why that
    # mattered.
    body = resp.json()
    assert body["error"]["code"] == "REQUEST_BUDGET_EXCEEDED"
    assert "detail" not in body


async def test_the_504_carries_the_canonical_envelope():
    """The only 504 this service emits itself, and it was unreadable.

    ``{"detail": "request timeout"}`` carries no code, so a client that
    branches on ``error.code`` — every other 4xx and 5xx on this surface,
    REST and MCP alike — saw nothing at all and could only report the
    wall-clock number. That is precisely what happened to the /recall and
    /search 504s on 2026-09-17: three probe attempts, CRUD healthy
    throughout, and no diagnosis to show for them.
    """
    app = _build_test_app(timeout_seconds=0.1)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/slow")

    err = resp.json()["error"]
    assert err["code"] == "REQUEST_BUDGET_EXCEEDED"
    assert err["details"]["budget_seconds"] == 0.1
    assert err["details"]["path"] == "/slow"
    # The elapsed time is the number the caller actually has to reason
    # about: it says whether the handler ran to the deadline or died early.
    assert err["details"]["elapsed_seconds"] >= 0.1


async def test_the_code_is_not_upstream_timeout():
    """``code_for_status(504)`` is ``UPSTREAM_TIMEOUT``, which is a claim
    about a backend. This 504 is the opposite: our own budget expired and
    we cancelled the handler, with nothing upstream having reported a
    failure. Reusing that code sends the caller after the wrong system."""
    from core_api.errors import code_for_status

    app = _build_test_app(timeout_seconds=0.1)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/slow")

    assert resp.json()["error"]["code"] != code_for_status(504)


async def test_the_504_says_how_soon_to_retry():
    """Same contract the per-tenant 429 got under D14: a response telling
    the caller to retry has to say how soon, or the caller picks a number."""
    app = _build_test_app(timeout_seconds=0.1)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/slow")

    assert resp.headers["retry-after"] == "1"


async def test_the_body_length_is_declared():
    """The old body was a fixed literal; this one varies with the budget and
    the path. A raw ASGI ``http.response.start`` sends exactly the headers
    given it — nothing downstream computes Content-Length — so a client
    reading to the declared length would hang or truncate without it."""
    app = _build_test_app(timeout_seconds=0.1)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/slow")

    assert int(resp.headers["content-length"]) == len(resp.content)


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
