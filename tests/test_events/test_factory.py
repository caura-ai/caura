"""`get_event_bus()` selector behaviour — backend resolution + errors."""

from __future__ import annotations

import pytest

from common.events import InProcessEventBus, PubSubEventBus, get_event_bus
from common.events.factory import reset_event_bus_for_testing

pytestmark = pytest.mark.asyncio


async def test_default_backend_is_inprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    await reset_event_bus_for_testing()
    monkeypatch.delenv("EVENT_BUS_BACKEND", raising=False)
    bus = get_event_bus()
    assert isinstance(bus, InProcessEventBus)


async def test_explicit_inprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    await reset_event_bus_for_testing()
    monkeypatch.setenv("EVENT_BUS_BACKEND", "inprocess")
    bus = get_event_bus()
    assert isinstance(bus, InProcessEventBus)


async def test_pubsub_backend_requires_project_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await reset_event_bus_for_testing()
    monkeypatch.setenv("EVENT_BUS_BACKEND", "pubsub")
    monkeypatch.delenv("GCP_PROJECT_ID", raising=False)
    monkeypatch.delenv("EVENT_BUS_PROJECT_ID", raising=False)
    monkeypatch.setenv("EVENT_BUS_SUBSCRIPTION_PREFIX", "core-api")
    with pytest.raises(RuntimeError, match="GCP_PROJECT_ID"):
        get_event_bus()


async def test_pubsub_backend_requires_subscription_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await reset_event_bus_for_testing()
    monkeypatch.setenv("EVENT_BUS_BACKEND", "pubsub")
    monkeypatch.setenv("GCP_PROJECT_ID", "proj")
    monkeypatch.delenv("EVENT_BUS_SUBSCRIPTION_PREFIX", raising=False)
    with pytest.raises(RuntimeError, match="EVENT_BUS_SUBSCRIPTION_PREFIX"):
        get_event_bus()


async def test_pubsub_backend_constructs_when_env_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await reset_event_bus_for_testing()
    monkeypatch.setenv("EVENT_BUS_BACKEND", "pubsub")
    monkeypatch.setenv("GCP_PROJECT_ID", "proj")
    monkeypatch.setenv("EVENT_BUS_SUBSCRIPTION_PREFIX", "core-api")
    bus = get_event_bus()
    assert isinstance(bus, PubSubEventBus)


async def test_pubsub_backend_fails_fast_when_sdk_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The factory calls _ensure_pubsub_sdk() at resolution time so a
    # missing SDK surfaces at service boot, not on first publish.
    import builtins

    original_import = builtins.__import__

    def blocked(name: str, *a: object, **kw: object) -> object:
        if name.startswith("google.cloud"):
            raise ImportError("blocked for test")
        return original_import(name, *a, **kw)

    await reset_event_bus_for_testing()
    monkeypatch.setenv("EVENT_BUS_BACKEND", "pubsub")
    monkeypatch.setenv("GCP_PROJECT_ID", "proj")
    monkeypatch.setenv("EVENT_BUS_SUBSCRIPTION_PREFIX", "core-api")
    monkeypatch.setattr(builtins, "__import__", blocked)

    with pytest.raises(RuntimeError, match="google-cloud-pubsub"):
        get_event_bus()


async def test_pubsub_backend_honors_max_messages_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await reset_event_bus_for_testing()
    monkeypatch.setenv("EVENT_BUS_BACKEND", "pubsub")
    monkeypatch.setenv("GCP_PROJECT_ID", "proj")
    monkeypatch.setenv("EVENT_BUS_SUBSCRIPTION_PREFIX", "core-api")
    monkeypatch.setenv("EVENT_BUS_PUBSUB_MAX_MESSAGES", "10")
    bus = get_event_bus()
    assert isinstance(bus, PubSubEventBus)
    assert bus._max_messages == 10


async def test_pubsub_backend_default_max_messages_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await reset_event_bus_for_testing()
    monkeypatch.setenv("EVENT_BUS_BACKEND", "pubsub")
    monkeypatch.setenv("GCP_PROJECT_ID", "proj")
    monkeypatch.setenv("EVENT_BUS_SUBSCRIPTION_PREFIX", "core-api")
    monkeypatch.delenv("EVENT_BUS_PUBSUB_MAX_MESSAGES", raising=False)
    bus = get_event_bus()
    assert isinstance(bus, PubSubEventBus)
    assert bus._max_messages == 25


async def test_pubsub_backend_accepts_max_messages_upper_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await reset_event_bus_for_testing()
    monkeypatch.setenv("EVENT_BUS_BACKEND", "pubsub")
    monkeypatch.setenv("GCP_PROJECT_ID", "proj")
    monkeypatch.setenv("EVENT_BUS_SUBSCRIPTION_PREFIX", "core-api")
    monkeypatch.setenv("EVENT_BUS_PUBSUB_MAX_MESSAGES", "1000")
    bus = get_event_bus()
    assert isinstance(bus, PubSubEventBus)
    assert bus._max_messages == 1000


@pytest.mark.parametrize("bad", ["abc", "0", "-3", "1.5", "1001"])
async def test_pubsub_backend_rejects_invalid_max_messages(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    await reset_event_bus_for_testing()
    monkeypatch.setenv("EVENT_BUS_BACKEND", "pubsub")
    monkeypatch.setenv("GCP_PROJECT_ID", "proj")
    monkeypatch.setenv("EVENT_BUS_SUBSCRIPTION_PREFIX", "core-api")
    monkeypatch.setenv("EVENT_BUS_PUBSUB_MAX_MESSAGES", bad)
    with pytest.raises(RuntimeError, match="EVENT_BUS_PUBSUB_MAX_MESSAGES"):
        get_event_bus()


async def test_unknown_backend_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    await reset_event_bus_for_testing()
    monkeypatch.setenv("EVENT_BUS_BACKEND", "rabbitmq")
    with pytest.raises(ValueError, match="Unknown EVENT_BUS_BACKEND"):
        get_event_bus()


async def test_singleton_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    await reset_event_bus_for_testing()
    monkeypatch.setenv("EVENT_BUS_BACKEND", "inprocess")
    assert get_event_bus() is get_event_bus()
