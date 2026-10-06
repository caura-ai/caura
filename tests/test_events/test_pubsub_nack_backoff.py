"""A nacked Pub/Sub message is redelivered after a growing delay, not at once.

The bus used to nack with ``modify_ack_deadline`` of 0. Some handlers raise on
purpose to be retried later — a lifecycle message whose claim is held by a run
still in progress raises ``claim_conflict`` — and with no subscription
``RetryPolicy`` a deadline of 0 brought that message straight back at
pull-loop speed, burning a ``max_delivery_attempts`` budget in seconds.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import common.events.pubsub as pubsub_module
from common.events import PubSubEventBus
from common.events.pubsub import LEASE_EXTENSION_SECONDS
from tests._legacy_contracts import frozen_topic

# Read through the module so the behaviour tests below still COLLECT against a
# bus that predates the schedule — and fail on what it does, not on an import.
_BASE = getattr(pubsub_module, "NACK_BACKOFF_BASE_SECONDS", 10)
_MAX = getattr(pubsub_module, "NACK_BACKOFF_MAX_SECONDS", 600)

_EVENT_BYTES = json.dumps({"event_type": frozen_topic("memory.embedded")}).encode()


@pytest.fixture
def bus() -> PubSubEventBus:
    return PubSubEventBus(
        project_id="proj", subscription_prefix="test", dual_subscribe=True
    )


def _received(ack_id: str, delivery_attempt: Any) -> Any:
    r = MagicMock()
    r.ack_id = ack_id
    r.delivery_attempt = delivery_attempt
    r.message.data = _EVENT_BYTES
    r.message.attributes = {}
    r.message.message_id = f"m-{ack_id}"
    return r


async def _drive(
    bus: PubSubEventBus, received: list[Any], outcomes: dict[str, bool]
) -> dict[str, Any]:
    """One pull batch through ``_pull_loop``; returns the ack and the
    ``modify_ack_deadline`` requests it issued (lease extensions excluded)."""
    acked: list[str] = []
    deadlines: list[tuple[int, list[str]]] = []
    fake = MagicMock()
    fake.subscription_path = lambda proj, sub: f"projects/{proj}/subscriptions/{sub}"
    calls = {"n": 0}

    def _pull(request: Any = None, timeout: Any = None) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            return MagicMock(received_messages=received)
        bus._stopping = True
        return MagicMock(received_messages=[])

    def _modify(request: dict[str, Any]) -> None:
        if request["ack_deadline_seconds"] != LEASE_EXTENSION_SECONDS:
            deadlines.append(
                (request["ack_deadline_seconds"], list(request["ack_ids"]))
            )

    fake.pull = MagicMock(side_effect=_pull)
    fake.acknowledge = MagicMock(
        side_effect=lambda request: acked.extend(request["ack_ids"])
    )
    fake.modify_ack_deadline = MagicMock(side_effect=_modify)
    bus._subscriber = fake
    bus._pull_executor = MagicMock()

    by_message = {f"m-{r.ack_id}": outcomes[r.ack_id] for r in received}
    seen: list[str] = []

    async def _dispatch(_handlers: Any, event: Any) -> bool:
        # One message per dispatch, in order — map back by position.
        mid = f"m-{received[len(seen)].ack_id}"
        seen.append(mid)
        return by_message[mid]

    loop = asyncio.get_running_loop()

    async def _direct(_executor: Any, fn: Any, *args: Any) -> Any:
        return fn(*args)

    with (
        patch.object(bus, "_dispatch_all", new=_dispatch),
        patch.object(loop, "run_in_executor", new=AsyncMock(side_effect=_direct)),
    ):
        await bus._pull_loop("test-sub", [lambda _e: None])
    return {"acked": acked, "deadlines": deadlines}


async def test_failed_message_is_not_nacked_with_deadline_zero(bus: PubSubEventBus):
    out = await _drive(bus, [_received("a", 0)], {"a": False})
    assert out["deadlines"], "the failed message was never nacked"
    assert all(d > 0 for d, _ in out["deadlines"]), out["deadlines"]
    assert out["deadlines"] == [(_BASE, ["a"])]


async def test_nack_delay_grows_with_delivery_attempt(bus: PubSubEventBus):
    received = [_received("a1", 1), _received("a3", 3), _received("a9", 9)]
    out = await _drive(bus, received, {"a1": False, "a3": False, "a9": False})
    got = {ids[0]: d for d, ids in out["deadlines"]}
    assert got == {
        "a1": _BASE,
        "a3": _BASE * 4,
        "a9": _MAX,
    }


async def test_acks_are_unaffected_and_same_delay_nacks_share_a_request(
    bus: PubSubEventBus,
):
    received = [_received("ok", 1), _received("f1", 2), _received("f2", 2)]
    out = await _drive(bus, received, {"ok": True, "f1": False, "f2": False})
    assert out["acked"] == ["ok"]
    assert out["deadlines"] == [(_BASE * 2, ["f1", "f2"])]


@pytest.mark.parametrize(
    ("attempt", "expected"),
    [
        (0, _BASE),  # not tracked by the subscription
        (None, _BASE),
        (MagicMock(), _BASE),
        (1, _BASE),
        (2, _BASE * 2),
        (5, min(_BASE * 16, _MAX)),
        (10_000, _MAX),
    ],
)
def test_nack_delay_schedule(attempt: Any, expected: int):
    assert pubsub_module._nack_delay_seconds(attempt) == expected


def test_schedule_never_collides_with_a_lease_extension():
    # The bus tells a nack from a lease refresh by its deadline on the wire;
    # the schedule must never produce the lease value.
    assert LEASE_EXTENSION_SECONDS not in {
        pubsub_module._nack_delay_seconds(n) for n in range(0, 200)
    }
    assert _MAX <= 600  # the API's ceiling
    assert pubsub_module.NACK_BACKOFF_BASE_SECONDS > 0
