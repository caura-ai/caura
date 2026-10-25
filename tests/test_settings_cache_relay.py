"""Settings invalidations reach sibling workers on the in-process bus (#663).

On the self-hosted stack the event bus is in-process and core-api runs two
uvicorn workers. The ``SETTINGS_CHANGED`` broadcast (CAURA-571) reached only
the worker that published it, so the other kept enforcing the old settings
for up to the 5-minute TTL: a disabled ``interviewer.enabled`` kept accepting
submissions there while a GET answered by the first worker showed it off.
``settings_cache_relay`` carries the invalidation over Redis instead.

Redis is replaced by an in-memory hub with the same publish / subscribe /
``get_message`` surface, so these run without a server; each listener stands
in for one worker's process.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock

import pytest

from common.events.inprocess import InProcessEventBus
from core_api.services import organization_settings as os_svc
from core_api.services import settings_cache_relay as relay

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_cache():
    os_svc._settings_cache.clear()
    yield
    os_svc._settings_cache.clear()


class _Hub:
    """Fan-out of PUBLISH to every live subscription, as Redis does.

    ``changed`` is set on every subscribe, publish and close, and by the
    workers below on every eviction, so a test waits for a state rather than
    sleeping until it probably holds.
    """

    def __init__(self) -> None:
        self.subscriptions: list[_PubSub] = []
        self.published: list[tuple[str, str]] = []
        self.changed = asyncio.Event()

    async def publish(self, channel: str, message: str) -> bool:
        self.published.append((channel, message))
        for sub in self.subscriptions:
            if channel in sub.channels and not sub.closed:
                sub.inbox.put_nowait(
                    {"type": "message", "channel": channel, "data": message}
                )
        self.changed.set()
        return True

    async def pubsub(self):
        return _PubSub(self)

    async def until(self, predicate) -> None:
        async with asyncio.timeout(2):
            while True:
                self.changed.clear()
                if predicate():
                    return
                await self.changed.wait()


class _PubSub:
    def __init__(self, hub: _Hub) -> None:
        self.hub = hub
        self.channels: set[str] = set()
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        self.channels.add(channel)
        self.hub.subscriptions.append(self)
        self.inbox.put_nowait({"type": "subscribe", "channel": channel, "data": 1})
        self.hub.changed.set()

    async def get_message(self, **options):
        """redis-py's ``get_message(ignore_subscribe_messages, timeout)``."""
        try:
            async with asyncio.timeout(options.get("timeout")):
                message = await self.inbox.get()
        except TimeoutError:
            return None
        if isinstance(message, Exception):
            raise message
        if options.get("ignore_subscribe_messages") and message["type"] == "subscribe":
            return None
        return message

    async def aclose(self) -> None:
        self.closed = True
        self.hub.changed.set()


@pytest.fixture
def hub(monkeypatch) -> _Hub:
    hub = _Hub()
    monkeypatch.setattr(relay, "cache_publish", hub.publish)
    monkeypatch.setattr(relay, "cache_pubsub", hub.pubsub)
    monkeypatch.setattr(relay, "_RETRY_SECONDS", 0.01)
    monkeypatch.setattr(relay, "_POLL_SECONDS", 0.01)
    return hub


@pytest.fixture
def in_process_stack(monkeypatch) -> None:
    """The self-hosted shape: REDIS_URL set, in-process event bus."""
    monkeypatch.setattr(relay.settings, "redis_url", "redis://redis:6379/0")
    monkeypatch.setattr(relay, "get_event_bus", lambda: InProcessEventBus())


@pytest.fixture
def shared_bus(monkeypatch) -> None:
    """A bus that already reaches every process, such as Pub/Sub."""
    monkeypatch.setattr(relay.settings, "redis_url", "redis://redis:6379/0")
    monkeypatch.setattr(relay, "get_event_bus", lambda: object())


class _Worker:
    """One process's settings cache, with the listener that serves it."""

    def __init__(self, hub: _Hub, cache: dict) -> None:
        self.hub = hub
        self.cache = cache
        self.clears = 0
        self.task = relay.start_listener(self.invalidate, self.clear)

    def invalidate(self, org_id: str) -> None:
        self.cache.pop(org_id, None)
        self.hub.changed.set()

    def clear(self) -> None:
        self.clears += 1
        self.hub.changed.set()

    async def stop(self) -> None:
        await relay.stop_listener(self.task)


def test_the_relay_runs_only_where_the_bus_cannot_reach_siblings(monkeypatch) -> None:
    monkeypatch.setattr(relay, "get_event_bus", lambda: InProcessEventBus())
    monkeypatch.setattr(relay.settings, "redis_url", "")
    assert relay.relay_enabled() is False  # no Redis to carry it

    monkeypatch.setattr(relay.settings, "redis_url", "redis://redis:6379/0")
    assert relay.relay_enabled() is True

    monkeypatch.setattr(relay, "get_event_bus", lambda: object())
    assert relay.relay_enabled() is False


async def test_a_write_on_one_worker_evicts_the_other_workers_copy(
    hub, in_process_stack
) -> None:
    first = _Worker(hub, {"org-1": "stale", "org-2": "kept"})
    second = _Worker(hub, {"org-1": "stale"})
    await hub.until(lambda: len(hub.subscriptions) == 2)

    await relay.publish_invalidation("org-1")

    await hub.until(lambda: "org-1" not in first.cache and "org-1" not in second.cache)
    assert first.cache == {"org-2": "kept"}
    assert hub.published == [(relay.CHANNEL, "org-1")]
    await first.stop()
    await second.stop()


async def test_a_shared_bus_gets_no_listener_and_no_publish(hub, shared_bus) -> None:
    assert relay.start_listener(lambda org_id: None, lambda: None) is None
    await relay.stop_listener(None)  # the shutdown path takes the None as is
    await relay.publish_invalidation("org-1")
    assert hub.subscriptions == []
    assert hub.published == []


async def test_a_refused_publish_is_logged_and_does_not_raise(
    in_process_stack, monkeypatch, caplog
) -> None:
    monkeypatch.setattr(relay, "cache_publish", AsyncMock(return_value=False))
    with caplog.at_level(logging.WARNING, logger=relay.__name__):
        await relay.publish_invalidation("org-1")
    assert "within the cache TTL" in caplog.text


async def test_each_subscription_starts_by_clearing_the_cache(
    hub, in_process_stack
) -> None:
    # Whatever was cached while the listener could not hear may be stale.
    worker = _Worker(hub, {})
    await hub.until(lambda: worker.clears == 1)
    await worker.stop()


async def test_a_broken_subscription_subscribes_again_and_clears_again(
    hub, in_process_stack
) -> None:
    worker = _Worker(hub, {"org-1": "stale"})
    await hub.until(lambda: len(hub.subscriptions) == 1)
    broken = hub.subscriptions[0]
    broken.inbox.put_nowait(ConnectionError("connection reset by peer"))

    await hub.until(lambda: len(hub.subscriptions) == 2 and worker.clears == 2)
    assert broken.closed
    await relay.publish_invalidation("org-1")
    await hub.until(lambda: "org-1" not in worker.cache)
    await worker.stop()


async def test_no_redis_at_startup_waits_and_retries(
    hub, in_process_stack, monkeypatch
) -> None:
    attempts: list[int] = []

    async def unavailable_then_up():
        attempts.append(1)
        return None if len(attempts) < 3 else await hub.pubsub()

    monkeypatch.setattr(relay, "cache_pubsub", unavailable_then_up)
    worker = _Worker(hub, {})
    await hub.until(lambda: worker.clears == 1)
    assert len(attempts) == 3
    await worker.stop()


async def test_stopping_the_listener_closes_its_subscription(
    hub, in_process_stack
) -> None:
    worker = _Worker(hub, {})
    await hub.until(lambda: len(hub.subscriptions) == 1)
    await worker.stop()
    assert worker.task.cancelled()
    assert hub.subscriptions[0].closed


async def test_update_settings_relays_the_invalidation(monkeypatch) -> None:
    sc = AsyncMock()
    sc.update_org_settings = AsyncMock(
        return_value={"settings": {"interviewer": {"enabled": False}}, "changed": True}
    )
    monkeypatch.setattr(os_svc, "get_storage_client", lambda: sc)
    monkeypatch.setattr(os_svc, "get_event_bus", lambda: AsyncMock())
    relayed = AsyncMock()
    monkeypatch.setattr(os_svc, "publish_invalidation", relayed)

    await os_svc.update_settings("t1", {"interviewer": {"enabled": False}})

    relayed.assert_awaited_once_with("t1")


async def test_an_unchanged_write_relays_nothing(monkeypatch) -> None:
    sc = AsyncMock()
    sc.update_org_settings = AsyncMock(return_value={"settings": {}, "changed": False})
    monkeypatch.setattr(os_svc, "get_storage_client", lambda: sc)
    relayed = AsyncMock()
    monkeypatch.setattr(os_svc, "publish_invalidation", relayed)

    await os_svc.update_settings("t1", {"interviewer": {"enabled": False}})

    relayed.assert_not_awaited()


def test_clear_cache_evicts_every_tenant() -> None:
    os_svc._settings_cache["t1"] = os_svc._CachedSettings({}, None)
    os_svc._settings_cache["t2"] = os_svc._CachedSettings({}, None)
    os_svc.clear_cache()
    assert len(os_svc._settings_cache) == 0
