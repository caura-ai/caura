"""Tests for RequestObservationMiddleware — per-endpoint API usage metrics.

Exercises the production middleware directly against a minimal FastAPI app
and captures the emitted ``http.request`` log records with ``caplog``. The
middleware logs via stdlib ``logging`` with ``extra={...}``, so each field
surfaces as an attribute on the captured ``LogRecord``.

Contract pinned here:
  * exactly one ``http.request`` event per request,
  * ``http_route`` is the TEMPLATE (``/things/{id}``), never the raw path,
  * 404s and pre-routing failures bucket to ``"unmatched"``,
  * a crashing endpoint still emits one event, defaulting to status 500,
  * duration reflects the full downstream call (streaming included),
  * ``tenant_id`` stashed on ``request.state`` reaches the event.
"""

import asyncio
import logging

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from httpx import ASGITransport, AsyncClient

from core_api.middleware.request_observation import RequestObservationMiddleware

pytestmark = pytest.mark.asyncio


def _build_test_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestObservationMiddleware)

    @app.get("/things/{thing_id}")
    async def get_thing(thing_id: str):
        return {"id": thing_id}

    @app.get("/boom")
    async def boom():
        raise RuntimeError("kaboom")

    @app.get("/tenant")
    async def tenant(request: Request):
        # Simulate what get_auth_context does once a caller is authenticated.
        request.state.tenant_id = "tenant-xyz"
        return {"ok": True}

    @app.get("/stream")
    async def stream():
        async def gen():
            yield b"a"
            await asyncio.sleep(0.15)
            yield b"b"

        return StreamingResponse(gen(), media_type="text/plain")

    return app


def _events(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage() == "http.request"]


async def _get(app, path: str):
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.get(path)


async def test_routed_request_emits_templated_route(caplog):
    app = _build_test_app()
    with caplog.at_level(logging.INFO, logger="core_api.access"):
        resp = await _get(app, "/things/42")
    assert resp.status_code == 200

    events = _events(caplog)
    assert len(events) == 1, f"expected exactly one event, got {len(events)}"
    e = events[0]
    # The template, NOT the raw path — this is the cardinality contract.
    assert e.http_route == "/things/{thing_id}"
    assert "42" not in e.http_route
    assert e.http_method == "GET"
    assert e.http_status_code == 200
    assert isinstance(e.http_duration_ms, float)
    assert e.tenant_id is None


async def test_404_buckets_to_unmatched(caplog):
    app = _build_test_app()
    with caplog.at_level(logging.INFO, logger="core_api.access"):
        resp = await _get(app, "/nope/does-not-exist")
    assert resp.status_code == 404

    events = _events(caplog)
    assert len(events) == 1
    assert events[0].http_route == "unmatched"
    assert events[0].http_status_code == 404


async def test_raising_endpoint_emits_one_event_status_500(caplog):
    app = _build_test_app()
    with caplog.at_level(logging.INFO, logger="core_api.access"):
        resp = await _get(app, "/boom")
    assert resp.status_code == 500

    events = _events(caplog)
    assert len(events) == 1, "a crash must still emit exactly one event"
    e = events[0]
    # Router sets scope["route"] before invoking the endpoint, so the
    # template is known even though the handler raised.
    assert e.http_route == "/boom"
    # No http.response.start reached our middleware → default 500.
    assert e.http_status_code == 500


async def test_tenant_id_from_request_state_reaches_event(caplog):
    app = _build_test_app()
    with caplog.at_level(logging.INFO, logger="core_api.access"):
        resp = await _get(app, "/tenant")
    assert resp.status_code == 200

    events = _events(caplog)
    assert len(events) == 1
    assert events[0].tenant_id == "tenant-xyz"


async def test_streaming_duration_covers_full_response(caplog):
    app = _build_test_app()
    with caplog.at_level(logging.INFO, logger="core_api.access"):
        resp = await _get(app, "/stream")
    assert resp.status_code == 200

    events = _events(caplog)
    assert len(events) == 1
    e = events[0]
    assert e.http_route == "/stream"
    # The generator sleeps 150ms mid-stream; duration must span it, proving
    # we measure to the final body chunk, not to response start.
    assert e.http_duration_ms >= 150.0
