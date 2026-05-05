"""Smoke tests for core-operations cron-tick fanout (CAURA-655)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from core_operations import tasks
from core_operations.config import settings


class _StubResponse:
    def __init__(self, status_code: int, body: dict[str, Any] | str) -> None:
        self.status_code = status_code
        self._body = body
        self.text = body if isinstance(body, str) else ""

    def json(self) -> dict[str, Any]:
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


class _StubAsyncClient:
    """Minimal httpx.AsyncClient drop-in. ``post`` returns the
    response queued at construction; ``raise_on_post`` simulates a
    network error.
    """

    def __init__(
        self,
        *,
        response: _StubResponse | None = None,
        raise_on_post: Exception | None = None,
    ) -> None:
        self._response = response
        self._raise = raise_on_post
        self.calls: list[tuple[str, dict | None]] = []

    async def __aenter__(self) -> _StubAsyncClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def post(self, url: str, *, headers: dict | None = None) -> _StubResponse:
        self.calls.append((url, headers))
        if self._raise is not None:
            raise self._raise
        assert self._response is not None
        return self._response


@asynccontextmanager
async def _patch_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    response: _StubResponse | None = None,
    raise_on_post: Exception | None = None,
) -> AsyncIterator[_StubAsyncClient]:
    stub = _StubAsyncClient(response=response, raise_on_post=raise_on_post)
    monkeypatch.setattr(
        tasks.httpx,
        "AsyncClient",
        lambda *a, **kw: stub,
    )
    yield stub


@pytest.mark.asyncio
async def test_archive_expired_tick_posts_to_fanout(monkeypatch: pytest.MonkeyPatch):
    settings.core_api_url = "http://core-api"
    settings.core_api_admin_api_key = "admin-key-xyz"

    response = _StubResponse(200, {"action": "archive-expired", "published": 3, "failed": 0})
    async with _patch_client(monkeypatch, response=response) as stub:
        await tasks.run_archive_expired_tick()

    assert len(stub.calls) == 1
    url, headers = stub.calls[0]
    assert url == "http://core-api/api/v1/admin/lifecycle/fanout/archive-expired"
    assert headers == {"X-API-Key": "admin-key-xyz"}


@pytest.mark.asyncio
async def test_archive_stale_tick_hits_correct_path(monkeypatch: pytest.MonkeyPatch):
    settings.core_api_url = "http://core-api"
    settings.core_api_admin_api_key = "admin-key-xyz"

    response = _StubResponse(200, {"action": "archive-stale", "published": 0, "failed": 0})
    async with _patch_client(monkeypatch, response=response) as stub:
        await tasks.run_archive_stale_tick()

    assert stub.calls[0][0].endswith("/admin/lifecycle/fanout/archive-stale")


@pytest.mark.asyncio
async def test_tick_swallows_non_2xx(monkeypatch: pytest.MonkeyPatch):
    """A non-2xx response must not raise — the scheduler retries on the
    next tick anyway, and re-raising would just produce duplicate
    stack traces in the on-call channel without changing behaviour.
    """
    settings.core_api_url = "http://core-api"
    settings.core_api_admin_api_key = "admin-key-xyz"

    response = _StubResponse(503, "upstream timeout")
    async with _patch_client(monkeypatch, response=response):
        await tasks.run_archive_expired_tick()  # no raise


@pytest.mark.asyncio
async def test_tick_swallows_network_error(monkeypatch: pytest.MonkeyPatch):
    settings.core_api_url = "http://core-api"
    settings.core_api_admin_api_key = "admin-key-xyz"

    async with _patch_client(monkeypatch, raise_on_post=httpx.ConnectError("offline")):
        await tasks.run_archive_expired_tick()  # no raise
