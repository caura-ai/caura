"""Upstream 5xx/429 → retryable 503 mapping (2026-07-19 error review #3).

A transient dependency failure (storage-api 5xx/429) must surface to the
caller as a retryable 503, not an unhandled INTERNAL_ERROR 500. Prod
2026-07: a single storage-writer 503 on POST /fleet/heartbeat bubbled
through ``_post``'s ``raise_for_status`` as an unhandled 500 and opened an
Error-Tracking issue; the plugin should have just retried next tick.
"""

from __future__ import annotations

import json

import httpx
import pytest
from starlette.requests import Request

from core_api.app import app, upstream_http_error_handler

pytestmark = pytest.mark.asyncio


def _request(path: str = "/api/v1/fleet/heartbeat", method: str = "POST") -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [],
            "query_string": b"",
        }
    )


def _status_error(status: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "http://storage-writer/api/v1/storage/fleet/nodes")
    resp = httpx.Response(status, request=req)
    return httpx.HTTPStatusError(str(status), request=req, response=resp)


async def test_handler_is_registered_for_httpx_status_error():
    assert httpx.HTTPStatusError in app.exception_handlers


@pytest.mark.parametrize("status", [500, 502, 503, 504, 429])
async def test_upstream_5xx_and_429_map_to_retryable_503(status: int):
    resp = await upstream_http_error_handler(_request(), _status_error(status))
    assert resp.status_code == 503
    body = json.loads(bytes(resp.body))
    assert body["error"]["code"] == "UNAVAILABLE"


@pytest.mark.parametrize("status", [400, 404, 409, 422])
async def test_upstream_4xx_is_reraised_unchanged(status: int):
    # A 4xx from an upstream means OUR request was malformed — a genuine bug,
    # not a transient blip. The handler must re-raise it unchanged so it
    # surfaces exactly as before (catch-all 500), and callers that deliberately
    # let a storage 4xx propagate (bulk-write atomicity) keep seeing the raw
    # HTTPStatusError rather than a swallowed response.
    err = _status_error(status)
    with pytest.raises(httpx.HTTPStatusError) as caught:
        await upstream_http_error_handler(_request(), err)
    assert caught.value is err
