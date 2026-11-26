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


def _status_error(
    status: int, url: str = "http://storage-writer/api/v1/storage/fleet/nodes"
) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", url)
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


# ---------------------------------------------------------------------------
# Search: a storage 5xx/429 reaches the handler instead of becoming a 500.
#
# The search pipeline's runner catches every non-HTTPException a step raises,
# and the search service used to answer any failed step with 500 "Search
# pipeline failed unexpectedly". So the reader's 504, sent when the scored
# search reaches its statement timeout, never reached the handler above: a
# search that timed out under load answered 500, and ``/search`` logged it
# with ``error: false``.
# ---------------------------------------------------------------------------

_SCORED_SEARCH_URL = "http://storage-reader/api/v1/storage/memories/scored-search"


def _search_pipeline_failing_with(monkeypatch, err: Exception) -> None:
    """Make the search pipeline's scored-search step raise ``err``, as its storage call does."""
    from core_api.pipeline.runner import Pipeline

    class _ScoredSearchStep:
        name = "execute_scored_search"

        async def execute(self, ctx):
            raise err

    monkeypatch.setattr(
        "core_api.pipeline.compositions.search.build_search_pipeline",
        lambda: Pipeline("search", [_ScoredSearchStep()]),
    )


@pytest.mark.unit
@pytest.mark.parametrize("status", [500, 502, 503, 504, 429])
async def test_search_reraises_a_retryable_storage_error(monkeypatch, status: int):
    from core_api.services.memory_service import _search_memories_pipeline

    err = _status_error(status, _SCORED_SEARCH_URL)
    _search_pipeline_failing_with(monkeypatch, err)
    with pytest.raises(httpx.HTTPStatusError) as caught:
        await _search_memories_pipeline(tenant_id="t1", query="q")
    assert caught.value is err


@pytest.mark.unit
@pytest.mark.parametrize("status", [400, 404, 422])
async def test_search_keeps_a_storage_4xx_a_500(monkeypatch, status: int):
    # Same split as the handler: a 4xx is a bug in the request core-api built,
    # and no retry fixes it.
    from fastapi import HTTPException

    from core_api.services.memory_service import _search_memories_pipeline

    _search_pipeline_failing_with(
        monkeypatch, _status_error(status, _SCORED_SEARCH_URL)
    )
    with pytest.raises(HTTPException) as caught:
        await _search_memories_pipeline(tenant_id="t1", query="q")
    assert caught.value.status_code == 500
    assert caught.value.detail == "Search pipeline failed unexpectedly"


async def test_search_route_answers_a_storage_timeout_with_retryable_503(
    client, monkeypatch
):
    from tests.conftest import get_test_auth

    _search_pipeline_failing_with(monkeypatch, _status_error(504, _SCORED_SEARCH_URL))
    tenant_id, headers = get_test_auth()
    resp = await client.post(
        "/api/v1/search", json={"tenant_id": tenant_id, "query": "q"}, headers=headers
    )
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "UNAVAILABLE"


async def test_mcp_recall_answers_a_storage_timeout_with_upstream_timeout(
    mcp_env, monkeypatch
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from core_api import mcp_server
    from tests._mcp_test_helpers import parse_envelope

    _search_pipeline_failing_with(monkeypatch, _status_error(504, _SCORED_SEARCH_URL))
    monkeypatch.setattr(
        "core_api.services.organization_settings.resolve_config",
        AsyncMock(
            return_value=SimpleNamespace(
                recall_enabled=False,
                recall_boost=False,
                graph_expand=False,
                entity_retrieval=True,
            )
        ),
    )
    monkeypatch.setattr(
        "core_api.clients.storage_client.CoreStorageClient.get_agent",
        AsyncMock(return_value=None),
    )

    payload = parse_envelope(await mcp_server.caura_recall(query="q"))
    assert payload["error"]["code"] == "UPSTREAM_TIMEOUT", payload
