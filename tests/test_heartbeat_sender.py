"""Delivery: one attempt, swallowed failures, and the counter resets only on 202."""

from __future__ import annotations

import asyncio
import json
import random
from types import SimpleNamespace

import httpx
import pytest

from core_api.heartbeat import clients
from core_api.heartbeat import sender as sender_mod
from core_api.heartbeat.identity import DeploymentIdentity
from core_api.heartbeat.sender import HeartbeatSender, check_endpoint_url

pytestmark = [pytest.mark.unit]

IDENT = DeploymentIdentity(
    deployment_id="6f0a3c2e-1b4d-4e8f-9a7c-2d5e8b1f4a90", deployment_token="ab" * 32
)
PAYLOAD = {"schema": 1, "product": "caura-server", "deployment_id": IDENT.deployment_id}


def _settings(url="https://telemetry.caura.ai/api/telemetry/heartbeat"):
    return SimpleNamespace(caura_telemetry_url=url, is_standalone=False)


@pytest.fixture(autouse=True)
def _clean_counter():
    clients.disable()
    yield
    clients.disable()
    sender_mod._reset_for_tests()


def _sender(monkeypatch, handler, url=None) -> HeartbeatSender:
    """A sender whose HTTP goes to ``handler`` and whose build is canned."""
    s = HeartbeatSender(_settings(url) if url else _settings(), version="3.16.0")

    async def _build():
        return IDENT, dict(PAYLOAD)

    s.build = _build  # type: ignore[method-assign]
    real_client = httpx.AsyncClient

    def _client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr(sender_mod.httpx, "AsyncClient", _client)
    return s


def _seed_counter():
    clients.enable()
    clients.record("caura-client-python/1.0.2")
    clients.record_mcp()
    assert clients.snapshot()["caura-client-python"] == 1
    assert clients.snapshot()["mcp"] == 1


# ── URL policy ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "https://telemetry.caura.ai/api/telemetry/heartbeat",
        "https://collector.example.internal:8443/beat",
        "http://localhost:8010/api/telemetry/heartbeat",
        "http://127.0.0.1:8010/x",
        "http://[::1]:8010/x",
    ],
)
def test_check_endpoint_url_accepts(url):
    check_endpoint_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://telemetry.caura.ai/api/telemetry/heartbeat",
        "http://collector.example.internal/beat",
        "ftp://localhost/x",
        "telemetry.caura.ai",
        "",
    ],
)
def test_check_endpoint_url_refuses_plain_http(url):
    with pytest.raises(ValueError):
        check_endpoint_url(url)


async def test_send_refuses_plain_http_without_building_a_client(monkeypatch):
    def _boom(**_k):
        raise AssertionError("no client for a refused URL")

    monkeypatch.setattr(sender_mod.httpx, "AsyncClient", _boom)
    s = HeartbeatSender(_settings("http://telemetry.caura.ai/x"))
    _seed_counter()
    assert await s.send_once() is None
    assert s.last_status is None
    assert clients.snapshot()["mcp"] == 1


# ── delivery ─────────────────────────────────────────────────────────────


async def test_accepted_send_resets_the_counter(monkeypatch):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(202, json={"ok": True})

    s = _sender(monkeypatch, handler)
    _seed_counter()
    status = await s.send_once()

    assert status == 202
    assert s.last_status == 202
    assert s.last_sent_at is not None
    assert (
        s.deployment_id is None
    )  # build() is canned here; identity is set by the real build
    assert seen["url"] == "https://telemetry.caura.ai/api/telemetry/heartbeat"
    assert seen["headers"]["authorization"] == f"Bearer {IDENT.deployment_token}"
    assert seen["headers"]["user-agent"] == "caura-server/3.16.0"
    assert seen["headers"]["content-type"] == "application/json"
    assert seen["body"] == PAYLOAD
    # Nothing else identifies the request.
    assert "cookie" not in seen["headers"]
    assert all(v == 0 for v in clients.snapshot().values())


@pytest.mark.parametrize("status", [200, 400, 401, 429, 500])
async def test_non_202_keeps_the_counter(monkeypatch, status):
    s = _sender(monkeypatch, lambda _r: httpx.Response(status))
    _seed_counter()
    assert await s.send_once() == status
    assert s.last_status == status
    assert clients.snapshot()["caura-client-python"] == 1
    assert clients.snapshot()["mcp"] == 1


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
        httpx.ConnectTimeout("dns"),
        httpx.RemoteProtocolError("reset"),
    ],
)
async def test_transport_errors_are_swallowed(monkeypatch, exc):
    def handler(_r):
        raise exc

    s = _sender(monkeypatch, handler)
    _seed_counter()
    assert await s.send_once() is None
    assert s.last_status is None
    assert s.last_sent_at is None
    assert clients.snapshot()["caura-client-python"] == 1


async def test_build_failure_is_swallowed(monkeypatch):
    def _boom(**_k):
        raise AssertionError("no HTTP when the payload could not be built")

    monkeypatch.setattr(sender_mod.httpx, "AsyncClient", _boom)
    s = HeartbeatSender(_settings())

    async def _build():
        raise RuntimeError("storage down")

    s.build = _build  # type: ignore[method-assign]
    assert await s.send_once() is None


async def test_client_timeout_is_five_seconds(monkeypatch):
    seen: dict = {}
    real_client = httpx.AsyncClient

    def _client(**kwargs):
        seen["timeout"] = kwargs.get("timeout")
        kwargs["transport"] = httpx.MockTransport(lambda _r: httpx.Response(202))
        return real_client(**kwargs)

    monkeypatch.setattr(sender_mod.httpx, "AsyncClient", _client)
    s = HeartbeatSender(_settings())

    async def _build():
        return IDENT, dict(PAYLOAD)

    s.build = _build  # type: ignore[method-assign]
    await s.send_once()
    assert seen["timeout"] == 5.0


# ── cadence ──────────────────────────────────────────────────────────────


def test_cadence_windows():
    s = HeartbeatSender(_settings(), rng=random.Random(7))
    for _ in range(200):
        first = s._first_delay()
        assert 300 <= first <= 360
        nxt = s._next_delay()
        assert 86_400 - 3_600 <= nxt <= 86_400 + 3_600


async def test_loop_survives_a_failing_cycle():
    class _Sender(HeartbeatSender):
        def __init__(self):
            super().__init__(_settings())
            self.calls = 0
            self.done = asyncio.Event()

        def _first_delay(self):
            return 0.0

        def _next_delay(self):
            return 0.0

        async def send_once(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("first cycle explodes")
            if self.calls >= 3:
                self.done.set()
                await asyncio.sleep(3600)
            return 202

    s = _Sender()
    task = asyncio.create_task(s._run())
    await asyncio.wait_for(s.done.wait(), timeout=5)
    assert s.calls == 3
    assert s.next_send_at is not None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_start_enables_the_counter_and_tracks_the_task(monkeypatch):
    tracked: list = []

    def _track(coro):
        coro.close()
        tracked.append(coro)
        return SimpleNamespace(cancel=lambda: None)

    monkeypatch.setattr("core_api.tasks.track_task", _track)
    s = HeartbeatSender(_settings())
    s.start()
    assert len(tracked) == 1
    assert clients.is_enabled() is True
