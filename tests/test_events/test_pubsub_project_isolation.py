"""Broadcast isolation keeps every transport operation in its chosen project."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from common.events import Event, PubSubEventBus
from common.events.factory import get_event_bus, reset_event_bus_for_testing

pytestmark = [pytest.mark.unit]

TOPIC_PROJECT = "topic-project"
BROADCAST_PROJECT = "isolated-broadcast"
SETTING = "EVENT_BUS_BROADCAST_SUBSCRIPTION_PROJECT_ID"


def make_bus(project: str | None = BROADCAST_PROJECT) -> PubSubEventBus:
    return PubSubEventBus(
        TOPIC_PROJECT,
        "test-worker",
        topic_prefix="staging",
        dual_subscribe=True,
        broadcast_subscription_project_id=project,
    )


def make_subscriber() -> MagicMock:
    subscriber = MagicMock()
    subscriber.subscription_path.side_effect = lambda project, name: (
        f"projects/{project}/subscriptions/{name}"
    )
    return subscriber


@pytest.mark.parametrize("value", [None, "", "   "])
def test_unset_or_blank_retains_original_project(value: str | None) -> None:
    assert make_bus(value)._broadcast_subscription_project_id == TOPIC_PROJECT


@pytest.mark.parametrize(
    "value",
    [
        TOPIC_PROJECT,
        "Uppercase-project",
        "short",
        "bad/project",
        "bad_project",
        "a" * 31,
    ],
)
def test_invalid_or_same_project_refused(value: str) -> None:
    with pytest.raises(ValueError, match="different from the topic project"):
        make_bus(value)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, "", "  ", " isolated-broadcast "])
async def test_factory_resolves_broadcast_project_only(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    await reset_event_bus_for_testing()
    monkeypatch.setenv("EVENT_BUS_BACKEND", "pubsub")
    monkeypatch.setenv("GCP_PROJECT_ID", TOPIC_PROJECT)
    monkeypatch.setenv("EVENT_BUS_SUBSCRIPTION_PREFIX", "test-worker")
    monkeypatch.setenv("EVENT_BUS_DUAL_SUBSCRIBE", "true")
    if value is None:
        monkeypatch.delenv(SETTING, raising=False)
    else:
        monkeypatch.setenv(SETTING, value)
    try:
        with patch.object(PubSubEventBus, "_ensure_pubsub_sdk"):
            bus = get_event_bus()
        assert isinstance(bus, PubSubEventBus)
        assert bus._project_id == TOPIC_PROJECT
        expected = BROADCAST_PROJECT if value and value.strip() else TOPIC_PROJECT
        assert bus._broadcast_subscription_project_id == expected
    finally:
        await reset_event_bus_for_testing()


@pytest.mark.asyncio
@pytest.mark.parametrize("project", [None, BROADCAST_PROJECT])
async def test_start_and_cleanup_route_only_broadcasts(project: str | None) -> None:
    bus = make_bus(project)
    subscriber = make_subscriber()
    bus._broadcast_slot_id = "worker-a"
    bus.subscribe("test.broadcast", AsyncMock(), broadcast=True)
    bus.subscribe("test.work", AsyncMock())
    sdk = SimpleNamespace(SubscriberClient=lambda: subscriber)
    expected = project or TOPIC_PROJECT
    try:
        with (
            patch.object(bus, "_ensure_pubsub_sdk", return_value=sdk),
            patch.object(bus, "_pull_loop", new=AsyncMock()) as pull,
        ):
            await bus.start()
            await asyncio.gather(*bus._pull_tasks)
        # Dual subscribe intentionally binds current and renamed aliases.
        names = {
            f"test-worker--staging--{brand}.broadcast--worker-a"
            for brand in ("test", "caura")
        }
        durable_names = {
            f"test-worker--staging--{brand}.work" for brand in ("test", "caura")
        }
        created = [
            call.kwargs["request"]
            for call in subscriber.create_subscription.call_args_list
        ]
        paths = {f"projects/{expected}/subscriptions/{name}" for name in names}
        assert len(created) == 2
        assert {request["name"] for request in created} == paths
        assert {request["topic"] for request in created} == {
            f"projects/{TOPIC_PROJECT}/topics/staging--{brand}.broadcast"
            for brand in ("test", "caura")
        }
        for request in created:
            assert request["ack_deadline_seconds"] == 30
            assert request["expiration_policy"]["ttl"]["seconds"] == 86400
        assert {call.args[0] for call in pull.await_args_list} == names | durable_names
        await bus.release_broadcast_subscriptions()
        deleted = subscriber.delete_subscription.call_args_list
        assert len(deleted) == 2
        assert {call.kwargs["request"]["subscription"] for call in deleted} == paths
        # Release clears the delete list, but a pull task scheduled afterwards
        # must still use the isolated project rather than the topic project.
        assert bus._broadcast_sub_paths == []
        routes = dict.fromkeys(names, expected)
        assert bus._broadcast_subscription_projects == routes
    finally:
        await bus.stop()
    assert bus._broadcast_subscription_projects == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("project", [None, BROADCAST_PROJECT])
async def test_create_failure_never_retries_in_topic_project(
    project: str | None,
) -> None:
    from google.api_core.exceptions import PermissionDenied

    bus = make_bus(project)
    subscriber = make_subscriber()
    subscriber.create_subscription.side_effect = PermissionDenied("denied")
    bus._broadcast_slot_id = "worker-a"
    bus.subscribe("test.broadcast", AsyncMock(), broadcast=True)
    sdk = SimpleNamespace(SubscriberClient=lambda: subscriber)
    try:
        with (
            patch.object(bus, "_ensure_pubsub_sdk", return_value=sdk),
            patch.object(bus, "_pull_loop", new=AsyncMock()) as pull,
        ):
            await bus.start()
        requests = [
            call.kwargs["request"]
            for call in subscriber.create_subscription.call_args_list
        ]
        assert len(requests) == 2  # One attempt for each broadcast alias.
        assert len({request["name"] for request in requests}) == 2
        assert all(
            request["name"].startswith(f"projects/{project or TOPIC_PROJECT}/")
            for request in requests
        )
        pull.assert_not_awaited()
        assert bus.is_healthy is (project is None)
    finally:
        await bus.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("broadcast", [True, False])
async def test_actual_pull_and_ack_keep_project_after_release(broadcast: bool) -> None:
    bus = make_bus()
    subscriber = make_subscriber()
    name = "test-worker--staging--test.broadcast--worker-a"
    expected = BROADCAST_PROJECT if broadcast else TOPIC_PROJECT
    if broadcast:
        bus._broadcast_subscription_projects[name] = BROADCAST_PROJECT
    bus._subscriber = subscriber
    bus._broadcast_sub_paths = []  # As after release, before a pending task starts.

    def pull(**_kwargs):
        bus._stopping = True
        return SimpleNamespace(
            received_messages=[
                SimpleNamespace(
                    ack_id="ack-one",
                    message=SimpleNamespace(data=b"{", attributes={}, message_id="one"),
                )
            ]
        )

    subscriber.pull.side_effect = pull
    with ThreadPoolExecutor(max_workers=1) as executor:
        bus._pull_executor = executor
        await bus._pull_loop(name, [])
    expected_path = f"projects/{expected}/subscriptions/{name}"
    assert subscriber.pull.call_args.kwargs["request"]["subscription"] == expected_path
    assert subscriber.acknowledge.call_args.kwargs["request"] == {
        "subscription": expected_path,
        "ack_ids": ["ack-one"],
    }


@pytest.mark.asyncio
async def test_publishing_keeps_source_topic_project() -> None:
    bus = make_bus()
    publisher = MagicMock(spec=["topic_path", "publish", "stop"])
    publisher.topic_path.side_effect = lambda project, topic: (
        f"projects/{project}/topics/{topic}"
    )
    publisher.publish.return_value.result.return_value = "message-one"
    bus._publisher = publisher
    try:
        await bus.publish("test.broadcast", Event(event_type="test.broadcast"))
        assert publisher.publish.call_args.args[0] == (
            f"projects/{TOPIC_PROJECT}/topics/staging--test.broadcast"
        )
    finally:
        await bus.stop()
