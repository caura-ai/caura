"""Delivery: one attempt, recorded failures, the counter resets only on 202, roles."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from types import SimpleNamespace

import httpx
import pytest

from core_api.heartbeat import clients
from core_api.heartbeat import sender as sender_mod
from core_api.heartbeat.identity import DeploymentIdentity
from core_api.heartbeat.sender import HeartbeatSender, describe_error
from core_api.heartbeat.state import SharedState

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


# ── URL guard (defence in depth; the policy refuses such a URL at boot) ──


async def test_send_refuses_plain_http_without_building_a_client(monkeypatch):
    def _boom(**_k):
        raise AssertionError("no client for a refused URL")

    monkeypatch.setattr(sender_mod.httpx, "AsyncClient", _boom)
    s = HeartbeatSender(_settings("http://telemetry.caura.ai/x"))
    _seed_counter()
    assert await s.send_once() is None
    assert s.last_status is None
    assert s.last_error is not None and s.last_error.startswith("invalid endpoint")
    assert s.last_attempt_at is not None
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
    assert s.last_attempt_at is not None
    assert s.last_error is None
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
    assert s.last_error == f"collector answered {status}"
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
async def test_transport_errors_are_recorded_not_raised(monkeypatch, caplog, exc):
    def handler(_r):
        raise exc

    s = _sender(monkeypatch, handler)
    _seed_counter()
    with caplog.at_level(logging.WARNING, logger="core_api.heartbeat"):
        assert await s.send_once() is None
    assert s.last_status is None
    assert s.last_sent_at is None
    # "Tried and failed" is visible: when, and a short URL-free reason.
    assert s.last_attempt_at is not None
    assert s.last_error is not None
    assert s.last_error.startswith(f"delivery failed: {type(exc).__name__}")
    assert "://" not in s.last_error
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1  # once per cycle, not per retry (there is none)
    assert "heartbeat not delivered" in warnings[0].getMessage()
    assert clients.snapshot()["caura-client-python"] == 1


def test_describe_error_is_short_and_url_free():
    err = httpx.ConnectError(
        "failed to connect to https://user:secret@collector.example.internal/beat"
    )
    text = describe_error(err)
    assert text.startswith("ConnectError:")
    assert "secret" not in text
    assert "<url>" in text
    assert len(describe_error(RuntimeError("x" * 1000))) <= 200


async def test_version_is_normalised_in_the_user_agent(monkeypatch):
    """``CAURA_VERSION=v3.17.0`` (the pinning form) must not leak the ``v``."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers["user-agent"]
        return httpx.Response(202)

    s = _sender(monkeypatch, handler)
    assert HeartbeatSender(_settings(), version="v3.17.0")._version == "3.17.0"
    s._version = sender_mod.normalise_version("v3.17.0")
    await s.send_once()
    assert seen["ua"] == "caura-server/3.17.0"


async def test_build_failure_is_swallowed(monkeypatch):
    def _boom(**_k):
        raise AssertionError("no HTTP when the payload could not be built")

    monkeypatch.setattr(sender_mod.httpx, "AsyncClient", _boom)
    s = HeartbeatSender(_settings())

    async def _build():
        raise RuntimeError("storage down")

    s.build = _build  # type: ignore[method-assign]
    assert await s.send_once() is None
    assert s.last_error is not None
    assert s.last_error.startswith("payload build failed: RuntimeError")


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


def _fake_track(monkeypatch) -> list:
    tracked: list = []

    def _track(coro):
        tracked.append(coro.__name__)
        coro.close()
        return SimpleNamespace(cancel=lambda: None)

    monkeypatch.setattr("core_api.tasks.track_task", _track)
    return tracked


def test_start_enables_the_counter_and_tracks_the_task(monkeypatch):
    tracked = _fake_track(monkeypatch)
    s = HeartbeatSender(_settings())
    s.start()
    assert tracked == ["_run"]
    assert s.role == "single"
    assert s.is_leader is True
    assert clients.is_enabled() is True


# ── roles ────────────────────────────────────────────────────────────────


def test_first_worker_leads_second_follows(monkeypatch, tmp_path):
    tracked = _fake_track(monkeypatch)
    leader = HeartbeatSender(_settings(), shared=SharedState(tmp_path))
    leader.start()
    assert leader.role == "leader"
    assert tracked == ["_run", "_sync"]  # the loop plus the flush/retry tick

    follower = HeartbeatSender(
        _settings(), shared=SharedState(tmp_path, pid=os.getppid())
    )
    follower.start()
    assert follower.role == "follower"
    assert follower.is_leader is False
    assert tracked == ["_run", "_sync", "_sync"]  # no second loop
    leader.shared.release_leader()


def test_follower_answers_status_from_the_leaders_state_file(tmp_path):
    leader = HeartbeatSender(_settings(), shared=SharedState(tmp_path))
    assert leader.shared.try_acquire_leader()
    leader.deployment_id = IDENT.deployment_id
    leader.last_status = 202
    leader.last_error = None
    leader.next_send_at = leader.last_sent_at = leader.last_attempt_at = (
        sender_mod.datetime(2026, 9, 19, 16, 23, 52, tzinfo=sender_mod.UTC)
    )
    leader._publish_status()

    follower = HeartbeatSender(
        _settings(), shared=SharedState(tmp_path, pid=os.getppid())
    )
    follower.last_status = 500  # its own memory is irrelevant
    assert follower.status() == leader.status()
    assert follower.status()["last_sent_at"] == "2026-09-19T16:23:52Z"
    assert follower.status()["deployment_id"] == IDENT.deployment_id
    leader.shared.release_leader()
    # No leader state yet: a follower falls back to its own (empty) fields.
    (tmp_path / "state.json").unlink()
    assert follower.status()["last_status"] == 500


async def test_follower_takes_over_when_the_lock_frees(tmp_path):
    """The sync tick retries the lock and starts the loop, resuming the schedule."""
    holder = SharedState(tmp_path, pid=os.getppid())
    assert holder.try_acquire_leader()
    holder.write_status({"next_send_at": "2999-01-01T00:00:00Z"})

    s = HeartbeatSender(
        _settings(),
        shared=SharedState(tmp_path),
        flush_interval=0.02,
        leader_retry=0.02,
        interval=3600.0,
    )
    s.start()
    assert s.role == "follower"
    await asyncio.sleep(0.1)
    assert s._task is None
    holder.release_leader()
    deadline = asyncio.get_running_loop().time() + 5
    while s._task is None:
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.02)
    assert s.role == "leader"
    await asyncio.sleep(0.05)
    # The resumed delay is bounded by one interval, so a far-future
    # next_send_at from the old leader cannot silence the new one for years.
    assert s.next_send_at is not None
    assert (
        s.next_send_at - sender_mod.datetime.now(sender_mod.UTC)
    ).total_seconds() <= 3600.5
    for task in (s._task, s._sync_task):
        task.cancel()
    await asyncio.gather(s._task, s._sync_task, return_exceptions=True)
    assert s.shared.is_leader is False  # released on cancellation


async def test_resume_delay_falls_back_to_the_first_delay_without_state(tmp_path):
    s = HeartbeatSender(_settings(), shared=SharedState(tmp_path), rng=random.Random(1))
    assert 300 <= s._resume_delay() <= 360
    s.shared.write_status({"next_send_at": "not-a-date"})
    assert 300 <= s._resume_delay() <= 360
