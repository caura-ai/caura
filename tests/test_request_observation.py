"""Tests for RequestObservationMiddleware — per-endpoint API usage metrics.

Exercises the production middleware directly against a minimal FastAPI app
and captures the emitted ``http.request`` log records with ``caplog``. The
middleware logs via stdlib ``logging`` with ``extra={...}``, so each field
surfaces as an attribute on the captured ``LogRecord``.

Contract pinned here:
  * exactly one ``http.request`` event per request,
  * ``http_route`` is the TEMPLATE (``/things/{id}``), never the raw path,
  * mounted sub-apps label by mount prefix (``/mcp``), not ``"unmatched"``,
  * 404s and pre-routing failures bucket to ``"unmatched"``,
  * a SUCCESSFUL liveness/readiness probe emits no event at all, while a
    failing one still does,
  * a crashing endpoint still emits one event, defaulting to status 500,
  * duration reflects the full downstream call (streaming included),
  * ``tenant_id`` stashed on ``request.state`` reaches the event.
"""

import asyncio
import logging

import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from httpx import ASGITransport, AsyncClient

from core_api.constants import PROBE_ROUTES, VERSION_PATH
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

    # A mounted sub-app is NOT a FastAPI route, so it never populates
    # scope["route"] — the case that used to disappear into "unmatched".
    async def _sub(scope, receive, send):
        await PlainTextResponse("ok")(scope, receive, send)

    app.mount("/mcp", _sub)
    app.mount("/static", _sub)

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


@pytest.mark.parametrize(
    "mount,path", [("/mcp", "/mcp/session"), ("/static", "/static/x.css")]
)
async def test_mounted_subapp_labels_by_mount_prefix(caplog, mount, path):
    """Mounted traffic must carry its mount prefix, not "unmatched".

    Only FastAPI's APIRoute sets ``scope["route"]``; a Starlette ``Mount``
    never does. So this traffic used to land in the same bucket as 404s: in
    6h of prod on 2026-08-29 that was 5,027 of 5,099 "unmatched" events, all
    of them successful, which made ~40% of the metric unattributable and hid
    the MCP transport from the per-endpoint dashboard.
    """
    app = _build_test_app()
    with caplog.at_level(logging.INFO, logger="core_api.access"):
        resp = await _get(app, path)
    assert resp.status_code == 200

    events = _events(caplog)
    assert len(events) == 1
    assert events[0].http_route == mount, (
        f"mounted traffic labelled {events[0].http_route!r}; it must carry the "
        f"mount prefix {mount!r} or it is indistinguishable from a 404"
    )
    # Still bounded: the prefix, never the raw path underneath it.
    assert events[0].http_route == mount and path != mount


async def test_unmatched_still_wins_when_there_is_no_mount(caplog):
    """The mount fallback must not swallow the genuine-404 label."""
    app = _build_test_app()
    with caplog.at_level(logging.INFO, logger="core_api.access"):
        resp = await _get(app, "/definitely/not/mounted")
    assert resp.status_code == 404
    assert _events(caplog)[0].http_route == "unmatched"


async def test_app_level_root_path_does_not_leak_into_the_label(caplog):
    """With the app served under a root_path, a 404 must stay "unmatched".

    The mount prefix is derived as the root_path DELTA across the downstream
    call, precisely so an app-level root_path (``--root-path /gw``) is not
    mistaken for a mount. Subtracting ``app_root_path`` instead would label
    this request ``/gw``.
    """
    app = _build_test_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False, root_path="/gw")
    with caplog.at_level(logging.INFO, logger="core_api.access"):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            await c.get("/nope")
    assert _events(caplog)[0].http_route == "unmatched"


# --- Probe suppression -------------------------------------------------
#
# These build the app the way app.py really does — a router included under
# ``prefix="/api/v1"`` — rather than hanging ``/health`` straight off the
# app. That difference is the whole point. Since FastAPI 0.137 an
# ``include_router(prefix=...)`` MOUNTS the router, so the label reaching
# ``http_route`` is the inner ``/health``, not the requested
# ``/api/v1/health``. A test that registered ``@app.get("/health")``
# directly would produce the label ``/health`` too, and would therefore
# pass whether or not the middleware handles the real prefixed case — the
# kind of green that means nothing.


def _build_probe_app(*, healthy: bool = True) -> FastAPI:
    router = APIRouter()

    @router.get("/health")
    async def health():
        if not healthy:
            return JSONResponse({"ok": False}, status_code=503)
        return {"ok": True}

    @router.get("/version")
    async def version():
        return {"version": "1"}

    @router.get("/things/{thing_id}")
    async def get_thing(thing_id: str):
        return {"id": thing_id}

    app = FastAPI()
    app.add_middleware(RequestObservationMiddleware)
    app.include_router(router, prefix="/api/v1")
    return app


@pytest.mark.parametrize("path", ["/api/v1/health", "/api/v1/version"])
async def test_successful_probe_emits_no_event(caplog, path):
    """A 200 from a liveness/readiness probe must not be logged at all."""
    app = _build_probe_app()
    with caplog.at_level(logging.INFO, logger="core_api.access"):
        resp = await _get(app, path)
    assert resp.status_code == 200
    events = _events(caplog)
    assert events == [], (
        f"{path} emitted {len(events)} event(s) "
        f"({[e.http_route for e in events]}); successful probes are "
        "infrastructure chatter and must be suppressed"
    )


async def test_failing_probe_is_still_logged(caplog):
    """A probe that FAILS is the one an investigation needs — keep it.

    Prod served 440 ``/health`` 503s in the week to 2026-08-29. Suppressing
    by route alone would have discarded every one of them.
    """
    app = _build_probe_app(healthy=False)
    with caplog.at_level(logging.INFO, logger="core_api.access"):
        resp = await _get(app, "/api/v1/health")
    assert resp.status_code == 503
    events = _events(caplog)
    assert len(events) == 1, (
        f"a failing probe emitted {len(events)} events; it must emit exactly "
        "one, otherwise the 503s vanish along with the healthy noise"
    )
    assert events[0].http_route == "/health"
    assert events[0].http_status_code == 503


async def test_prefixed_non_probe_route_still_logs_its_stripped_label(caplog):
    """Pins the label space the suppression list is written against.

    ``PROBE_ROUTES`` holds ``/health``, not ``/api/v1/health``, because
    FastAPI mounts prefixed routers. If a future FastAPI went back to
    flattening prefixes, ``http_route`` here would become
    ``/api/v1/things/{thing_id}`` — and the suppression above would silently
    stop matching anything. This assertion turns that into a red test rather
    than a quiet return of the probe traffic.
    """
    app = _build_probe_app()
    with caplog.at_level(logging.INFO, logger="core_api.access"):
        resp = await _get(app, "/api/v1/things/42")
    assert resp.status_code == 200
    events = _events(caplog)
    assert len(events) == 1
    assert events[0].http_route == "/things/{thing_id}", (
        f"prefixed route labelled {events[0].http_route!r}; PROBE_ROUTES is "
        "written in this label space and must be updated together with it"
    )


async def test_real_health_router_probe_is_suppressed(caplog):
    """The decisive one: the REAL router, not a hand-built stand-in.

    Every other test here builds its own ``/health``, so all of them would
    stay green if ``routes/health.py`` renamed the endpoint or app.py changed
    how it is registered. This imports the production router and serves it
    the way ``app.py`` does, so it answers the only question that matters:
    does the traffic prod actually generates get suppressed?

    ``/version`` is the probe to drive here because it has no dependencies —
    ``/health`` returns 503 without redis/storage, which is a valid response
    but tests the failure path rather than the suppression path.
    """
    from core_api.routes.health import router as real_health_router

    app = FastAPI()
    app.add_middleware(RequestObservationMiddleware)
    app.include_router(real_health_router, prefix="/api/v1")

    with caplog.at_level(logging.INFO, logger="core_api.access"):
        resp = await _get(app, "/api/v1/version")
    assert resp.status_code == 200, "the real /version route did not serve"
    assert _events(caplog) == [], (
        "the REAL /api/v1/version emitted an access event; the label it "
        f"produces is not in PROBE_ROUTES ({sorted(PROBE_ROUTES)}) and probe "
        "traffic is still being logged in production"
    )


async def test_probe_constants_are_the_ones_the_router_registers():
    """PROBE_ROUTES and the route decorators must be the same strings.

    They already are by construction — ``routes/health.py`` decorates with
    these very constants — and this asserts that construction still holds, so
    reintroducing a literal path in either place is caught here.
    """
    from core_api.routes.health import router as real_health_router

    declared = {r.path for r in real_health_router.routes if hasattr(r, "methods")}
    missing = PROBE_ROUTES - declared
    assert not missing, (
        f"{sorted(missing)} is suppressed from the access log but is not a "
        f"path the health router registers ({sorted(declared)}) — the "
        "suppression matches nothing and probe traffic is being logged"
    )
    assert VERSION_PATH in PROBE_ROUTES


async def test_no_other_router_declares_a_probe_path():
    """Suppression matches a bare label, so only ONE router may declare it.

    ``PROBE_ROUTES`` holds router-relative templates (``/health``), and the
    middleware matches on that string alone with no notion of which router
    produced it. A second router declaring its own ``/health`` — an admin
    sub-app, say — would therefore have its successful traffic silently
    suppressed too, and nothing else in the system would say so.

    Rather than couple the middleware to the health router at runtime, pin
    the assumption here: exactly one declaration per probe path, across the
    whole real app.

    The walk descends through FastAPI's ``_IncludedRouter`` via
    ``original_router``, since ``include_router(prefix=...)`` mounts the
    router opaquely (``app.py`` documents the same obstacle). That is a
    private attribute, so the count assertions below double as a check that
    the walk still works: if it stops descending, the probes are found zero
    times and this test fails rather than quietly passing.
    """
    import collections

    from core_api.app import app

    counts: collections.Counter = collections.Counter()

    def walk(routes):
        for r in routes:
            inner = getattr(r, "original_router", None)
            if inner is not None:
                walk(inner.routes)
                continue
            path = getattr(r, "path", None)
            if path is not None and hasattr(r, "methods"):
                counts[path] += 1

    walk(app.routes)

    for probe in sorted(PROBE_ROUTES):
        assert counts[probe] == 1, (
            f"{probe!r} is declared by {counts[probe]} route(s) in the real "
            f"app; the access-log suppression matches this label with no "
            f"regard for which router owns it, so anything other than exactly "
            f"1 means either the walk stopped working (0) or a second router "
            f"is having its successful traffic silently dropped (>1)"
        )
