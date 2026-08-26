"""PubSubEventBus behaviour with the SDK mocked out.

Covers the parts that live in our code — envelope encoding, decode
robustness, ack/nack selection on handler outcome. The SDK-facing calls
are replaced by stand-ins so these tests run without GCP.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import multiprocessing
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Empty
from typing import Any
from unittest.mock import MagicMock

import pytest

from common.events import Event, PubSubEventBus, Topics
from common.events.pubsub import (
    BROADCAST_MAX_SLOTS,
    BROADCAST_SUBSCRIPTION_TTL_SECONDS,
    _claim_broadcast_slot,
    _process_broadcast_slot_id,
)


@pytest.fixture
def bus() -> PubSubEventBus:
    b = PubSubEventBus(project_id="proj", subscription_prefix="test")
    # Pre-install a fake publisher so publish() doesn't touch the SDK.
    # spec-limited to the real PublisherClient surface we rely on: a
    # permissive MagicMock happily accepts .close(), which is exactly
    # how the stop()-calls-nonexistent-close() bug survived these tests
    # (PublisherClient has stop(), not close()).
    fake_publisher = MagicMock(spec=["topic_path", "publish", "stop"])
    fake_publisher.topic_path = lambda proj, topic: f"projects/{proj}/topics/{topic}"
    future = MagicMock()
    future.result = MagicMock(return_value="msg-id-1")
    fake_publisher.publish = MagicMock(return_value=future)
    b._publisher = fake_publisher
    return b


async def test_publish_encodes_envelope_as_json(bus: PubSubEventBus) -> None:
    event = Event(
        event_type=Topics.Memory.EMBED_REQUESTED,
        tenant_id="t1",
        payload={"memory_id": "abc"},
    )
    await bus.publish(Topics.Memory.EMBED_REQUESTED, event)

    bus._publisher.publish.assert_called_once()
    topic_path, data = bus._publisher.publish.call_args[0]
    assert topic_path == "projects/proj/topics/memclaw.memory.embed-requested"
    parsed = json.loads(data.decode())
    assert parsed["event_type"] == Topics.Memory.EMBED_REQUESTED
    assert parsed["tenant_id"] == "t1"
    assert parsed["payload"] == {"memory_id": "abc"}


async def test_topic_prefix_scopes_publish(bus: PubSubEventBus) -> None:
    # With an env-scoped topic prefix set, publish targets the prefixed topic id —
    # so an env's publishers/subscribers stay isolated from another env sharing the
    # GCP project (cross-env fan-out fix).
    bus._topic_prefix = "prod"
    event = Event(
        event_type=Topics.Memory.EMBEDDED, tenant_id="t1", payload={"memory_id": "abc"}
    )
    await bus.publish(Topics.Memory.EMBEDDED, event)
    topic_path, _ = bus._publisher.publish.call_args[0]
    assert topic_path == "projects/proj/topics/prod--memclaw.memory.embedded"


def test_topic_name_prefix_and_no_op() -> None:
    scoped = PubSubEventBus(
        project_id="proj", subscription_prefix="prod-core-api", topic_prefix="prod"
    )
    assert (
        scoped._topic_name("memclaw.memory.embedded") == "prod--memclaw.memory.embedded"
    )
    # Empty/unset prefix ⇒ the raw topic name (byte-identical to today's behaviour).
    noop = PubSubEventBus(project_id="proj", subscription_prefix="test")
    assert noop._topic_name("memclaw.memory.embedded") == "memclaw.memory.embedded"


async def test_decode_accepts_well_formed_envelope() -> None:
    src = Event(event_type=Topics.Memory.CREATED, tenant_id="t1", payload={"k": "v"})
    bytes_ = src.model_dump_json().encode("utf-8")
    decoded = PubSubEventBus._decode(bytes_)
    assert decoded is not None
    assert decoded.event_type == src.event_type
    assert decoded.tenant_id == "t1"
    assert decoded.payload == {"k": "v"}


async def test_decode_returns_none_on_garbage() -> None:
    assert PubSubEventBus._decode(b"not json at all") is None
    assert PubSubEventBus._decode(b'{"missing": "event_type"}') is None


async def test_dispatch_all_returns_true_when_all_handlers_succeed(
    bus: PubSubEventBus,
) -> None:
    called = 0

    async def h1(_e: Event) -> None:
        nonlocal called
        called += 1

    async def h2(_e: Event) -> None:
        nonlocal called
        called += 1

    result = await bus._dispatch_all([h1, h2], Event(event_type=Topics.Memory.CREATED))
    assert result is True
    assert called == 2


async def test_dispatch_all_returns_false_when_any_handler_raises(
    bus: PubSubEventBus,
) -> None:
    async def bad(_e: Event) -> None:
        raise RuntimeError("intentional test failure")

    async def good(_e: Event) -> None:
        pass

    result = await bus._dispatch_all(
        [good, bad], Event(event_type=Topics.Memory.CREATED)
    )
    assert result is False


async def test_dispatch_all_reraises_cancellation(bus: PubSubEventBus) -> None:
    # ``asyncio.CancelledError`` is a BaseException subclass (Py 3.8+)
    # so ``gather(return_exceptions=True)`` converts it to a returned
    # value rather than re-raising. Without an explicit CancelledError
    # branch in _dispatch_all, ``isinstance(result, Exception)`` misses
    # it and the message is silently acked — the handler was cancelled
    # mid-run and never completed, but Pub/Sub would mark it done.
    # _dispatch_all re-raises so the pull loop unwinds cleanly on
    # shutdown.
    async def handler(_e: Event) -> None:
        raise asyncio.CancelledError("simulated stop()")

    with pytest.raises(asyncio.CancelledError):
        await bus._dispatch_all([handler], Event(event_type=Topics.Memory.CREATED))


async def test_dispatch_all_logs_all_exceptions_before_reraising_cancellation(
    bus: PubSubEventBus, caplog: pytest.LogCaptureFixture
) -> None:
    """Mixed batch: a CancelledError and an Exception from different
    handlers. The cancellation must propagate (so the pull loop
    unwinds), but the Exception still has to be logged — eager re-raise
    on first cancellation would silently drop the failure log."""

    async def cancelled_handler(_e: Event) -> None:
        raise asyncio.CancelledError("simulated stop()")

    async def failing_handler(_e: Event) -> None:
        raise RuntimeError("genuine handler bug")

    with caplog.at_level("ERROR"), pytest.raises(asyncio.CancelledError):
        await bus._dispatch_all(
            [cancelled_handler, failing_handler],
            Event(event_type=Topics.Memory.CREATED),
        )

    assert any(
        "genuine handler bug" in (rec.exc_info[1].args[0] if rec.exc_info else "")
        for rec in caplog.records
    ), "RuntimeError must be logged before CancelledError propagates"


async def test_dispatch_all_runs_every_handler_even_after_earlier_raise(
    bus: PubSubEventBus,
) -> None:
    # Mirrors InProcessEventBus semantics: one handler's exception must
    # not prevent subsequent handlers from running. This is the
    # cross-backend contract that makes code validated against the
    # inprocess bus behave identically on Pub/Sub.
    ran: list[str] = []

    async def bad_first(_e: Event) -> None:
        ran.append("bad_first")
        raise RuntimeError("intentional test failure")

    async def good_after_bad(_e: Event) -> None:
        ran.append("good_after_bad")

    async def bad_last(_e: Event) -> None:
        ran.append("bad_last")
        raise RuntimeError("another intentional failure")

    result = await bus._dispatch_all(
        [bad_first, good_after_bad, bad_last],
        Event(event_type=Topics.Memory.CREATED),
    )
    assert ran == ["bad_first", "good_after_bad", "bad_last"]
    assert result is False


async def test_ensure_pubsub_sdk_raises_runtime_error_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Block the import so we can exercise the graceful-failure path even
    # on environments where the SDK is present.
    import builtins

    original_import = builtins.__import__

    def blocked(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("google.cloud"):
            raise ImportError("blocked for test")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(RuntimeError, match="google-cloud-pubsub"):
        PubSubEventBus._ensure_pubsub_sdk()


async def test_subscribe_before_start_records_handlers(bus: PubSubEventBus) -> None:
    async def h(_e: Event) -> None:
        return None

    bus.subscribe(Topics.Memory.CREATED, h)
    bus.subscribe(Topics.Memory.CREATED, h)
    bus.subscribe(Topics.Memory.EMBEDDED, h)

    assert len(bus._handlers[Topics.Memory.CREATED]) == 2
    assert len(bus._handlers[Topics.Memory.EMBEDDED]) == 1
    # No SDK was touched — start() isn't called here.
    assert bus._subscriber is None


async def test_stop_cancels_pending_pull_tasks_cleanly(bus: PubSubEventBus) -> None:
    # Simulate an in-flight pull by planting a sleeping task in the
    # bus's pull-task list. stop() must cancel + await it without
    # propagating CancelledError.
    async def long_sleep() -> None:
        # 60s is intentional: it must exceed any conceivable test
        # runtime so the task NEVER completes naturally — a regression
        # in stop()'s cancellation path is detected by the outer
        # ``wait_for`` timing out rather than the sleep completing.
        await asyncio.sleep(60)

    task = asyncio.create_task(long_sleep())
    bus._pull_tasks.append(task)
    # Audit T4: bound the test's runtime against a regression in
    # ``stop()`` cancellation. Without this, a future change that
    # silently drops the cancel call would hang the test for the full
    # ``sleep(60)`` duration instead of failing within seconds. The
    # production contract is "stop() returns quickly after cancelling
    # all pull tasks" — 2s is a generous ceiling on that.
    await asyncio.wait_for(bus.stop(), timeout=2.0)
    assert task.cancelled() or task.done()


async def test_stop_closes_publisher_and_subscriber(bus: PubSubEventBus) -> None:
    # Both clients own gRPC channels + threads; stop() must shut each
    # down via its REAL API and null the references out. The APIs are
    # asymmetric: SubscriberClient has close(); PublisherClient has
    # stop() (commits outstanding batches + joins the commit thread —
    # the only flush in the pipeline, since publish() is fire-and-
    # forget). The fixture's publisher mock is spec-limited so calling
    # a nonexistent close() on it would raise instead of silently
    # passing.
    fake_subscriber = MagicMock()
    bus._subscriber = fake_subscriber
    # bus._publisher is already a spec-limited MagicMock from the fixture.
    publisher_ref = bus._publisher

    await bus.stop()

    publisher_ref.stop.assert_called_once()
    fake_subscriber.close.assert_called_once()
    assert bus._publisher is None
    assert bus._subscriber is None


async def test_stop_teardown_order(
    bus: PubSubEventBus,
) -> None:
    # Pull and publish sides have opposite tradeoffs during shutdown:
    #   - PULL:    close subscriber FIRST so blocking pull() calls wake
    #              immediately with a closed channel, then drain exec.
    #              `_pull_loop` short-circuits on `_stopping` so the
    #              gRPC error is absorbed quietly.
    #   - PUBLISH: drain exec FIRST so in-flight publish() threads
    #              complete on a live channel, then publisher.stop()
    #              commits the outstanding client batches. Reverse
    #              order would lose messages already en route.
    # Expected call order:
    #   subscriber.close → pull-exec.shutdown
    #     → publish-exec.shutdown → publisher.stop
    calls: list[str] = []

    pull_exec = MagicMock()
    pull_exec.shutdown = MagicMock(
        side_effect=lambda wait: calls.append("pull-exec.shutdown"),
    )
    pub_exec = MagicMock()
    pub_exec.shutdown = MagicMock(
        side_effect=lambda wait: calls.append("pub-exec.shutdown"),
    )
    bus._pull_executor = pull_exec
    bus._publish_executor = pub_exec

    fake_subscriber = MagicMock()
    fake_subscriber.close = MagicMock(
        side_effect=lambda: calls.append("subscriber.close"),
    )
    bus._subscriber = fake_subscriber

    publisher = bus._publisher
    publisher.stop = MagicMock(side_effect=lambda: calls.append("publisher.stop"))

    await bus.stop()

    assert calls == [
        "subscriber.close",
        "pull-exec.shutdown",
        "pub-exec.shutdown",
        "publisher.stop",
    ]
    pull_exec.shutdown.assert_called_once_with(True)
    pub_exec.shutdown.assert_called_once_with(True)
    # Executor attrs cleared before their awaits, so a concurrent call
    # that lands mid-shutdown lazy-inits a fresh one instead of racing.
    assert bus._publish_executor is None
    assert bus._pull_executor is None


async def test_stop_allows_clean_restart(bus: PubSubEventBus) -> None:
    # After stop(), the bus must be reusable: pull_tasks cleared, the
    # stopping flag reset, subscribe() accepting new handlers again,
    # and the executor recreated on next access. Without these resets,
    # subscribe() would raise and start() would silently no-op — the
    # bus becomes permanently defunct after one lifecycle.
    async def noop() -> None:
        return None

    # Simulate "bus has started once already"
    bus._pull_tasks.append(asyncio.create_task(noop()))
    bus._stopping = True
    original_executor = bus._get_publish_executor()

    await bus.stop()

    assert bus._pull_tasks == []
    assert bus._stopping is False
    assert bus._publish_executor is None

    async def handler(_e: Event) -> None:
        return None

    # subscribe() no longer raises; the guard reads _pull_tasks which is
    # now empty.
    bus.subscribe(Topics.Memory.CREATED, handler)
    # Lazy access recreates a fresh executor — different object than
    # the one we got before stop().
    new_executor = bus._get_publish_executor()
    assert new_executor is not original_executor
    assert not new_executor._shutdown  # noqa: SLF001 — internal check


async def test_publish_uses_bounded_executor(bus: PubSubEventBus) -> None:
    # Reach into the lazy-init to confirm we don't fall back on asyncio's
    # default executor (which is effectively unbounded).
    ex = bus._get_publish_executor()
    assert ex._max_workers == 32  # noqa: SLF001 — internal state check


async def test_subscribe_after_start_raises(bus: PubSubEventBus) -> None:
    # Simulate start() having already run by flipping ``_started``.
    # A publisher-only bus ends up with empty ``_pull_tasks`` even
    # post-start, so the guard uses ``_started`` as the authoritative
    # signal — same field ``start()`` checks for its own idempotency.
    bus._started = True

    async def handler(_e: Event) -> None:
        return None

    with pytest.raises(RuntimeError, match="before start"):
        bus.subscribe(Topics.Memory.CREATED, handler)


async def test_subscribe_after_start_raises_even_for_publisher_only(
    bus: PubSubEventBus,
) -> None:
    """Regression guard: the old subscribe() guard used ``_pull_tasks``
    which is empty for publisher-only buses — a late subscribe() on
    such a bus would silently orphan the handler. The switch to
    ``_started`` catches this case."""
    bus._started = True
    # Publisher-only: no pull tasks exist, but start() HAS run.
    assert bus._pull_tasks == []

    async def handler(_e: Event) -> None:
        return None

    with pytest.raises(RuntimeError, match="before start"):
        bus.subscribe(Topics.Memory.CREATED, handler)


async def test_start_is_idempotent(bus: PubSubEventBus) -> None:
    # A second start() must not leak the first SubscriberClient or
    # spawn duplicate pull tasks.
    async def noop() -> None:
        return None

    # Pretend start() already ran. The ``_started`` flag is what the
    # idempotency guard checks — a publisher-only bus ends up with
    # empty ``_pull_tasks`` even after a successful start, so the
    # older guard form silently re-ran.
    sentinel_subscriber = MagicMock()
    bus._subscriber = sentinel_subscriber
    bus._pull_tasks.append(asyncio.create_task(noop()))
    bus._started = True

    await bus.start()

    # Same subscriber instance — start() did not replace it.
    assert bus._subscriber is sentinel_subscriber
    assert len(bus._pull_tasks) == 1

    await asyncio.gather(*bus._pull_tasks, return_exceptions=True)


async def test_publish_warns_when_subscribers_registered_without_start(
    bus: PubSubEventBus, caplog: pytest.LogCaptureFixture
) -> None:
    # Service that subscribes but forgets to await start() never receives
    # events — a silent misconfiguration. First publish should log a
    # warning, subsequent ones stay quiet.
    async def handler(_e: Event) -> None:
        return None

    bus.subscribe(Topics.Memory.CREATED, handler)

    with caplog.at_level("WARNING"):
        await bus.publish(
            Topics.Memory.CREATED, Event(event_type=Topics.Memory.CREATED)
        )
        first_warnings = [
            r for r in caplog.records if "start() was never awaited" in r.message
        ]
        await bus.publish(
            Topics.Memory.CREATED, Event(event_type=Topics.Memory.CREATED)
        )
        all_warnings = [
            r for r in caplog.records if "start() was never awaited" in r.message
        ]

    assert len(first_warnings) == 1
    assert len(all_warnings) == 1  # Didn't repeat on second publish


async def test_publish_does_not_warn_when_no_subscribers_registered(
    bus: PubSubEventBus, caplog: pytest.LogCaptureFixture
) -> None:
    # Publisher-only service (no subscribe calls) is a legitimate pattern
    # — no warning there.
    with caplog.at_level("WARNING"):
        await bus.publish(
            Topics.Memory.CREATED, Event(event_type=Topics.Memory.CREATED)
        )
    assert not any("start()" in r.message for r in caplog.records)


async def test_stop_resets_missing_start_warning_flag(bus: PubSubEventBus) -> None:
    # If the bus is stopped + reused, a fresh misconfiguration on the
    # second lifecycle must re-fire the warning. Previously this flag
    # was sticky across stop(), silencing the second cycle entirely.
    bus._warned_missing_start = True
    await bus.stop()
    assert bus._warned_missing_start is False


async def test_stop_keeps_pull_tasks_populated_through_teardown(
    bus: PubSubEventBus,
) -> None:
    # A concurrent start() during stop() must see `_pull_tasks` as
    # non-empty so the idempotency guard keeps it from creating a fresh
    # subscriber that stop() would then close out from under it. Assert
    # _pull_tasks stays populated until *after* the last close() call.

    async def dummy_task() -> None:
        return None

    seen_during_teardown: list[int] = []

    def record_len_during_close() -> None:
        seen_during_teardown.append(len(bus._pull_tasks))

    # Plant a cancelled-completed task so the cancel/gather at the top
    # of stop() doesn't block, then capture len(_pull_tasks) from each
    # close()/shutdown hook.
    completed = asyncio.create_task(dummy_task())
    await completed
    bus._pull_tasks.append(completed)

    exec_mock = MagicMock()
    exec_mock.shutdown = MagicMock(side_effect=lambda wait: record_len_during_close())
    bus._publish_executor = exec_mock

    sub_mock = MagicMock()
    sub_mock.close = MagicMock(side_effect=record_len_during_close)
    bus._subscriber = sub_mock

    bus._publisher.stop = MagicMock(side_effect=record_len_during_close)

    await bus.stop()

    # Every teardown step saw _pull_tasks still populated.
    assert seen_during_teardown == [1, 1, 1]
    # Only cleared at the very end.
    assert bus._pull_tasks == []


async def test_get_publish_executor_refuses_while_stopped(bus: PubSubEventBus) -> None:
    # Simulate a concurrent publish() landing after stop() flipped the
    # flag: _get_publish_executor must refuse instead of spinning up a
    # new pool that stop()'s teardown would never join.
    bus._publish_executor = None  # force lazy-init path
    bus._stopped = True
    with pytest.raises(RuntimeError, match="stopped"):
        bus._get_publish_executor()


async def test_pull_loop_refuses_when_bus_stopped(bus: PubSubEventBus) -> None:
    # Same guard on the pull side: _pull_loop must raise rather than
    # set up a new pull sequence after stop() begins.
    bus._stopped = True

    async def noop(_e: Event) -> None:
        return None

    with pytest.raises(RuntimeError, match="stopped"):
        await bus._pull_loop("sub-x", [noop])


async def test_stop_resets_stopped_flag_for_restart(bus: PubSubEventBus) -> None:
    # After stop() completes, the flag resets so the bus can be
    # restarted cleanly (matches the existing restart contract).
    await bus.stop()
    assert bus._stopped is False


async def test_is_healthy_reflects_failed_subscriptions(bus: PubSubEventBus) -> None:
    # Fresh bus is healthy.
    assert bus.is_healthy is True
    # Simulate a pull loop hitting a permanent error on subscription X.
    bus._failed_subscriptions.add("sub-x")
    assert bus.is_healthy is False
    # A restart via stop() clears the failed-subscription set.
    await bus.stop()
    assert bus.is_healthy is True


async def test_is_healthy_false_while_stop_in_progress(bus: PubSubEventBus) -> None:
    """``_stopped`` flips True at the top of ``stop()`` before any await
    point. is_healthy must observe that immediately so health probes
    during graceful shutdown don't still see green while the bus is
    actively tearing down."""
    bus._stopped = True
    assert bus.is_healthy is False
    # Resetting _stopped (end of stop()) returns control to the other
    # checks — with no handlers + no failures, the bus is healthy again.
    bus._stopped = False
    assert bus.is_healthy is True


async def test_is_healthy_false_when_handlers_registered_but_not_started(
    bus: PubSubEventBus,
) -> None:
    """Handlers without ``start()`` = pull loops don't exist → every
    inbound event is silently dropped. is_healthy must surface this
    as unhealthy so the readiness probe catches it. Regression guard
    for a review finding where is_healthy only checked
    ``_failed_subscriptions`` (empty in this scenario)."""

    async def _handler(_event):
        return None

    bus.subscribe("test-topic", _handler)
    # start() was NOT awaited.
    assert bus.is_healthy is False


async def test_start_constructs_subscriber_off_event_loop(
    bus: PubSubEventBus,
) -> None:
    """SubscriberClient construction can trigger Workload Identity
    credential refresh (metadata-server round trip) which blocks the
    asyncio event loop on service boot. start() must offload it to
    ``run_in_executor``. PublisherClient is NOT constructed here — a
    subscriber-only service shouldn't hold an unused publisher."""
    import types
    from unittest.mock import AsyncMock, patch

    async def _h(_e: Event) -> None:
        return None

    bus.subscribe(Topics.Memory.CREATED, _h)

    sub_ctor_calls = 0

    class FakePublisherClient:
        def __init__(self):
            raise AssertionError("PublisherClient must NOT be constructed in start()")

    class FakeSubscriberClient:
        def __init__(self):
            nonlocal sub_ctor_calls
            sub_ctor_calls += 1

    fake_sdk = types.SimpleNamespace(
        PublisherClient=FakePublisherClient,
        SubscriberClient=FakeSubscriberClient,
    )

    bus._publisher = None  # would explode if start() touched it

    offloaded: list[str] = []

    async def _spy_run_in_executor(executor, fn, *args):
        offloaded.append(fn.__name__)
        return fn(*args)

    loop = asyncio.get_running_loop()
    with (
        patch.object(PubSubEventBus, "_ensure_pubsub_sdk", return_value=fake_sdk),
        patch.object(
            loop, "run_in_executor", new=AsyncMock(side_effect=_spy_run_in_executor)
        ),
        patch.object(bus, "_pull_loop", new=AsyncMock()),
    ):
        await bus.start()

    assert sub_ctor_calls == 1
    assert "FakeSubscriberClient" in offloaded
    assert bus._publisher is None, "publisher must stay lazy"


async def test_publish_constructs_publisher_off_event_loop(
    bus: PubSubEventBus,
) -> None:
    """Same Workload-Identity concern as the subscriber, but for the
    publisher. Lazy + off-loop: the first publish() pays the cost via
    ``run_in_executor``; subsequent publishes hit the cached client."""
    import types
    from unittest.mock import AsyncMock, patch

    pub_ctor_calls = 0

    class FakePublisherClient:
        def __init__(self):
            nonlocal pub_ctor_calls
            pub_ctor_calls += 1

        def topic_path(self, project, topic):
            return f"projects/{project}/topics/{topic}"

        def publish(self, topic_path, data):
            future = MagicMock()
            future.result = MagicMock(return_value="msg-id-1")
            return future

    fake_sdk = types.SimpleNamespace(PublisherClient=FakePublisherClient)

    offloaded: list[str] = []
    real_run = asyncio.get_running_loop().run_in_executor

    async def _spy_run_in_executor(executor, fn, *args):
        offloaded.append(getattr(fn, "__name__", type(fn).__name__))
        return await real_run(executor, fn, *args)

    bus._publisher = None  # force first-publish construction path

    loop = asyncio.get_running_loop()
    with (
        patch.object(PubSubEventBus, "_ensure_pubsub_sdk", return_value=fake_sdk),
        patch.object(
            loop, "run_in_executor", new=AsyncMock(side_effect=_spy_run_in_executor)
        ),
    ):
        await bus.publish(
            Topics.Memory.EMBED_REQUESTED,
            Event(event_type=Topics.Memory.EMBED_REQUESTED, tenant_id="t1"),
        )
        await bus.publish(
            Topics.Memory.EMBED_REQUESTED,
            Event(event_type=Topics.Memory.EMBED_REQUESTED, tenant_id="t1"),
        )

    assert pub_ctor_calls == 1, "publisher must be constructed exactly once"
    assert "FakePublisherClient" in offloaded


async def test_ensure_publisher_closes_losing_candidate_on_toctou_race(
    bus: PubSubEventBus,
) -> None:
    """Concurrent first-publish callers can both pass the nil-check and
    both build a PublisherClient. The loser's gRPC channel + flush
    thread must be explicitly closed — non-deterministic GC isn't an
    acceptable cleanup story for a Cloud-Run-resident service."""
    import types
    from unittest.mock import AsyncMock, patch

    closed: list[object] = []

    class FakePublisherClient:
        def __init__(self):
            self.stopped = False

        def stop(self):
            # The real PublisherClient teardown API (it has no close()).
            self.stopped = True
            closed.append(self)

    fake_sdk = types.SimpleNamespace(PublisherClient=FakePublisherClient)

    bus._publisher = None
    # Simulate the race: while the first ``run_in_executor`` is
    # awaiting, populate ``bus._publisher`` from a "concurrent" caller.
    winning = FakePublisherClient()

    real_run = asyncio.get_running_loop().run_in_executor

    async def _race_run_in_executor(executor, fn, *args):
        # First call (PublisherClient()): inject the winner before the
        # await resolves so the second nil-check finds it populated.
        if fn is FakePublisherClient:
            bus._publisher = winning
        return await real_run(executor, fn, *args)

    loop = asyncio.get_running_loop()
    with (
        patch.object(PubSubEventBus, "_ensure_pubsub_sdk", return_value=fake_sdk),
        patch.object(
            loop, "run_in_executor", new=AsyncMock(side_effect=_race_run_in_executor)
        ),
    ):
        # Snapshot pending tasks so we can identify (and await) the
        # fire-and-forget close-loser task spawned inside
        # ``_ensure_publisher``. Sleep-loop polling is flaky under CI
        # (executor-thread scheduling latency varies); awaiting the
        # specific task is deterministic.
        before = asyncio.all_tasks()
        result = await bus._ensure_publisher()
        spawned = asyncio.all_tasks() - before - {asyncio.current_task()}
        for t in spawned:
            await t

    assert result is winning, "the pre-populated client must win"
    assert len(closed) == 1, "loser must be explicitly closed"
    assert closed[0] is not winning, "winner must NOT be closed"


async def test_ensure_publisher_returns_winner_when_stop_races_close(
    bus: PubSubEventBus,
) -> None:
    """3-way race: TOCTOU loser awaits ``candidate.stop()`` and during
    that yield ``stop()`` clears ``_publisher``. The loser must return
    the captured winner — not the now-None ``_publisher`` — otherwise
    ``publish()`` crashes on ``None.topic_path(...)``."""
    import types
    from unittest.mock import AsyncMock, patch

    class FakePublisherClient:
        def __init__(self):
            self.stopped = False

        def stop(self):
            # The real PublisherClient teardown API (it has no close()).
            self.stopped = True

    fake_sdk = types.SimpleNamespace(PublisherClient=FakePublisherClient)
    bus._publisher = None
    winning = FakePublisherClient()

    real_run = asyncio.get_running_loop().run_in_executor

    async def _race_run_in_executor(executor, fn, *args):
        if fn is FakePublisherClient:
            # Inject the winner before the candidate-construction await resolves.
            bus._publisher = winning
            return await real_run(executor, fn, *args)
        # Second call is candidate.stop() — simulate stop() racing in.
        bus._publisher = None
        return await real_run(executor, fn, *args)

    loop = asyncio.get_running_loop()
    with (
        patch.object(PubSubEventBus, "_ensure_pubsub_sdk", return_value=fake_sdk),
        patch.object(
            loop, "run_in_executor", new=AsyncMock(side_effect=_race_run_in_executor)
        ),
    ):
        result = await bus._ensure_publisher()

    assert result is winning, (
        "must return the captured winner even when stop() nulled _publisher mid-close"
    )


async def test_start_aborts_when_stop_races_subscriber_construction(
    bus: PubSubEventBus,
) -> None:
    """The new ``await loop.run_in_executor(None, SubscriberClient)`` is
    the first yield in ``start()``. If ``stop()`` flips ``_stopped``
    during this window, ``start()`` must close the just-constructed
    subscriber AND raise — silently returning would let a lifespan
    handler think the bus is operational while ``is_healthy`` slowly
    flips False on the next probe."""
    import types
    from unittest.mock import AsyncMock, patch

    sub_close_calls = 0

    class FakeSubscriberClient:
        def close(self):
            nonlocal sub_close_calls
            sub_close_calls += 1

    fake_sdk = types.SimpleNamespace(SubscriberClient=FakeSubscriberClient)

    async def _h(_e: Event) -> None:
        return None

    bus.subscribe(Topics.Memory.CREATED, _h)

    real_run = asyncio.get_running_loop().run_in_executor

    async def _stop_during_subscriber_construct(executor, fn, *args):
        if fn is FakeSubscriberClient:
            # Simulate a concurrent stop() completing while start() is
            # awaiting the SubscriberClient executor call.
            bus._stopped = True
        return await real_run(executor, fn, *args)

    loop = asyncio.get_running_loop()
    with (
        patch.object(PubSubEventBus, "_ensure_pubsub_sdk", return_value=fake_sdk),
        patch.object(
            loop,
            "run_in_executor",
            new=AsyncMock(side_effect=_stop_during_subscriber_construct),
        ),
        pytest.raises(RuntimeError, match="aborted: stop\\(\\) ran concurrently"),
    ):
        await bus.start()

    assert bus._subscriber is None, "raced subscriber must be cleared"
    assert sub_close_calls == 1, "raced subscriber must be explicitly closed"
    assert bus._pull_executor is None, "pull executor must NOT be created"
    assert bus._pull_tasks == [], "pull tasks must NOT be spawned"


async def test_pull_loop_records_failed_subscription_on_unexpected_cancellation(
    bus: PubSubEventBus,
) -> None:
    """A handler that raises CancelledError outside ``stop()`` (programming
    error: awaited a separately-cancelled task) must propagate AND mark
    the subscription failed so ``is_healthy`` flips False — silently
    halting consumption is the worst possible failure mode."""
    from unittest.mock import AsyncMock, patch

    async def cancelling_dispatch(_handlers, _event):
        raise asyncio.CancelledError("not from stop()")

    fake_subscriber = MagicMock()
    fake_subscriber.subscription_path = lambda proj, sub: (
        f"projects/{proj}/subscriptions/{sub}"
    )
    # Pull returns one fake message so dispatch fires.
    fake_msg = MagicMock()
    fake_msg.message.data = b'{"event_type": "memclaw.memory.created"}'
    fake_msg.ack_id = "ack-1"
    fake_response = MagicMock(received_messages=[fake_msg])
    fake_subscriber.pull = MagicMock(return_value=fake_response)

    bus._subscriber = fake_subscriber
    bus._pull_executor = MagicMock()
    bus._pull_executor.shutdown = MagicMock()

    loop = asyncio.get_running_loop()

    async def _direct_run(_executor, fn, *args):
        return fn(*args) if not args else fn(*args)

    with (
        patch.object(bus, "_dispatch_all", new=cancelling_dispatch),
        patch.object(loop, "run_in_executor", new=AsyncMock(side_effect=_direct_run)),
        pytest.raises(asyncio.CancelledError),
    ):
        await bus._pull_loop("test-sub", [lambda _e: None])

    assert "test-sub" in bus._failed_subscriptions
    assert bus.is_healthy is False


async def test_start_idempotency_guard_uses_started_flag(bus: PubSubEventBus) -> None:
    """Publisher-only bus has empty ``_pull_tasks`` even after start().
    The idempotency guard must check ``_started`` instead — otherwise a
    second start() silently re-runs the sequence. Regression guard for
    a review finding."""
    from unittest.mock import patch

    # Simulate a completed publisher-only start(): no handlers, no
    # pull tasks, _started flipped. Avoids depending on the real
    # Pub/Sub SDK being installed (the first start() would need it).
    bus._started = True
    assert bus._pull_tasks == []  # publisher-only confirmation

    # Second start() must short-circuit via the ``_started`` guard.
    # We detect a silent re-run by asserting _ensure_pubsub_sdk is
    # never invoked — if the guard used the old ``_pull_tasks`` check,
    # this would fire.
    with patch.object(
        PubSubEventBus, "_ensure_pubsub_sdk", side_effect=AssertionError("re-ran!")
    ):
        await bus.start()  # must not raise


async def test_dispatch_all_runs_handlers_concurrently(bus: PubSubEventBus) -> None:
    # Sequential dispatch would sum the sleeps; concurrent keeps total
    # wall time close to the slowest handler. This guards against a
    # regression that serialises handlers.
    import time

    async def slow(_e: Event) -> None:
        await asyncio.sleep(0.05)

    t0 = time.perf_counter()
    result = await bus._dispatch_all(
        [slow, slow, slow, slow], Event(event_type=Topics.Memory.CREATED)
    )
    elapsed = time.perf_counter() - t0
    assert result is True
    # Concurrent: ≈ 0.05 s total. Sequential: ≈ 0.20 s. 0.15 is a generous bound.
    assert elapsed < 0.15


async def test_constructor_tunables_default_and_override() -> None:
    default = PubSubEventBus(project_id="proj", subscription_prefix="test")
    assert default._max_messages == 25
    assert default._pull_timeout == 20.0
    assert default._error_backoff == 5.0

    custom = PubSubEventBus(
        project_id="proj",
        subscription_prefix="test",
        max_messages=100,
        pull_timeout=5.0,
        error_backoff=1.0,
    )
    assert custom._max_messages == 100
    assert custom._pull_timeout == 5.0
    assert custom._error_backoff == 1.0


async def test_decode_handles_pydantic_validation_error() -> None:
    # Valid JSON that doesn't match the Event schema must be dropped
    # (returns None), not propagate — otherwise _pull_loop backs off
    # without acking and Pub/Sub redelivers forever.
    import json as _json

    # Missing the required `event_type` field.
    bad_payload = _json.dumps({"tenant_id": "t1", "payload": {}}).encode()
    assert PubSubEventBus._decode(bad_payload) is None

    # Wrong type for `occurred_at` — invalid datetime string.
    bad_ts = _json.dumps(
        {"event_type": "memclaw.memory.created", "occurred_at": "not-a-date"}
    ).encode()
    assert PubSubEventBus._decode(bad_ts) is None


# ---------------------------------------------------------------------------
# Cross-environment fan-out guard
#
# Two environments sharing one GCP project share its topic namespace, so
# Pub/Sub fans every message out to *both* envs' subscriptions. The bus
# stamps a ``source_env`` attribute on publish and drops foreign-env copies
# in ``_pull_loop`` before they reach a handler.
# ---------------------------------------------------------------------------


def _make_received(data: bytes, ack_id: str, attributes: dict[str, str]) -> Any:
    """Build a fake Pub/Sub ReceivedMessage with real-dict attributes.

    A bare ``MagicMock`` would make ``message.attributes.get(...)`` return a
    truthy mock, defeating the guard's "attribute absent" branch — so the
    attributes must be a real mapping.
    """
    received = MagicMock()
    received.ack_id = ack_id
    received.message.data = data
    received.message.attributes = attributes
    return received


async def _drive_one_batch(bus: PubSubEventBus, received: list[Any]) -> dict[str, Any]:
    """Run ``_pull_loop`` for exactly one batch and return what happened.

    Returns ``{"dispatched": [...events], "acked": [...ack_ids],
    "nacked": [...ack_ids]}``. The first pull yields *received*; the second
    flips ``_stopping`` and returns nothing so the loop exits cleanly.
    """
    from unittest.mock import AsyncMock, patch

    dispatched: list[Event] = []

    async def recording_dispatch(_handlers: Any, event: Event) -> bool:
        dispatched.append(event)
        return True

    acked: list[str] = []
    nacked: list[str] = []

    fake_subscriber = MagicMock()
    fake_subscriber.subscription_path = lambda proj, sub: (
        f"projects/{proj}/subscriptions/{sub}"
    )

    calls = {"n": 0}

    def fake_pull(request: Any = None, timeout: Any = None) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            return MagicMock(received_messages=received)
        bus._stopping = True
        return MagicMock(received_messages=[])

    fake_subscriber.pull = MagicMock(side_effect=fake_pull)
    fake_subscriber.acknowledge = MagicMock(
        side_effect=lambda request: acked.extend(request["ack_ids"])
    )
    fake_subscriber.modify_ack_deadline = MagicMock(
        side_effect=lambda request: nacked.extend(request["ack_ids"])
    )

    bus._subscriber = fake_subscriber
    bus._pull_executor = MagicMock()

    loop = asyncio.get_running_loop()

    async def _direct_run(_executor: Any, fn: Any, *args: Any) -> Any:
        return fn(*args)

    with (
        patch.object(bus, "_dispatch_all", new=recording_dispatch),
        patch.object(loop, "run_in_executor", new=AsyncMock(side_effect=_direct_run)),
    ):
        await bus._pull_loop("test-sub", [lambda _e: None])

    return {"dispatched": dispatched, "acked": acked, "nacked": nacked}


async def test_publish_stamps_source_env_attribute() -> None:
    bus = PubSubEventBus(
        project_id="proj", subscription_prefix="test", env="production"
    )
    fake_publisher = MagicMock()
    fake_publisher.topic_path = lambda proj, topic: f"projects/{proj}/topics/{topic}"
    fake_publisher.publish = MagicMock(return_value=MagicMock())
    bus._publisher = fake_publisher

    await bus.publish(
        Topics.Memory.EMBED_REQUESTED,
        Event(event_type=Topics.Memory.EMBED_REQUESTED, payload={"memory_id": "abc"}),
    )

    # Attribute rides as a kwarg, leaving the positional (topic, data)
    # wire format intact.
    assert fake_publisher.publish.call_args.kwargs == {"source_env": "production"}


async def test_publish_omits_source_env_when_env_unset(bus: PubSubEventBus) -> None:
    # The shared fixture constructs the bus without an env.
    await bus.publish(
        Topics.Memory.EMBED_REQUESTED,
        Event(event_type=Topics.Memory.EMBED_REQUESTED),
    )
    assert "source_env" not in bus._publisher.publish.call_args.kwargs


async def test_env_is_normalised_and_empty_collapses_to_none() -> None:
    assert (
        PubSubEventBus(project_id="p", subscription_prefix="s", env=" production ")._env
        == "production"
    )
    assert (
        PubSubEventBus(project_id="p", subscription_prefix="s", env="   ")._env is None
    )
    assert PubSubEventBus(project_id="p", subscription_prefix="s", env="")._env is None
    assert PubSubEventBus(project_id="p", subscription_prefix="s")._env is None


async def test_foreign_source_env_decision_matrix() -> None:
    prod = PubSubEventBus(project_id="p", subscription_prefix="s", env="production")
    # Guard disabled when this bus has no env.
    no_env = PubSubEventBus(project_id="p", subscription_prefix="s")

    def msg(attrs: dict[str, str]) -> Any:
        m = MagicMock()
        m.attributes = attrs
        return m

    # Foreign → returns the offending env (drop).
    assert prod._foreign_source_env(msg({"source_env": "sandbox"})) == "sandbox"
    # Same env → None (process).
    assert prod._foreign_source_env(msg({"source_env": "production"})) is None
    # Attribute absent → None (backward-compatible, process).
    assert prod._foreign_source_env(msg({})) is None
    # Bus has no env → None regardless of the attribute.
    assert no_env._foreign_source_env(msg({"source_env": "sandbox"})) is None


async def test_pull_loop_drops_foreign_env_message_before_dispatch() -> None:
    bus = PubSubEventBus(
        project_id="proj", subscription_prefix="test", env="production"
    )
    foreign = _make_received(
        b'{"event_type": "memclaw.memory.embedded"}',
        "ack-foreign",
        {"source_env": "sandbox"},
    )

    result = await _drive_one_batch(bus, [foreign])

    # Never handled (no wasted provider call), acked so it isn't redelivered.
    assert result["dispatched"] == []
    assert result["acked"] == ["ack-foreign"]
    assert result["nacked"] == []


async def test_pull_loop_processes_same_env_message() -> None:
    bus = PubSubEventBus(
        project_id="proj", subscription_prefix="test", env="production"
    )
    local = _make_received(
        b'{"event_type": "memclaw.memory.embedded"}',
        "ack-local",
        {"source_env": "production"},
    )

    result = await _drive_one_batch(bus, [local])

    assert len(result["dispatched"]) == 1
    assert result["acked"] == ["ack-local"]


async def test_pull_loop_processes_message_without_source_env_attribute() -> None:
    # A publisher that predates the attribute (or an external producer) must
    # still be processed — the guard only drops *provably* foreign messages.
    bus = PubSubEventBus(
        project_id="proj", subscription_prefix="test", env="production"
    )
    legacy = _make_received(
        b'{"event_type": "memclaw.memory.embedded"}', "ack-legacy", {}
    )

    result = await _drive_one_batch(bus, [legacy])

    assert len(result["dispatched"]) == 1
    assert result["acked"] == ["ack-legacy"]


# ── broadcast subscriptions (CAURA-571) ──────────────────────────────


def test_subscribe_broadcast_records_topic() -> None:
    # broadcast=True flags the topic for a per-process subscription; the
    # default keeps the shared work-queue subscription.
    b = PubSubEventBus(project_id="proj", subscription_prefix="core-api")

    async def handler(event: Event) -> None: ...

    b.subscribe(Topics.Org.SETTINGS_CHANGED, handler, broadcast=True)
    b.subscribe(Topics.Memory.EMBEDDED, handler)
    assert Topics.Org.SETTINGS_CHANGED in b._broadcast_topics
    assert Topics.Memory.EMBEDDED not in b._broadcast_topics


async def test_ensure_broadcast_subscription_creates_with_expiration(
    bus: PubSubEventBus,
) -> None:
    # Each process creates its OWN subscription (unique name) so every process
    # receives every event; expiration_policy reaps it if the process dies.
    fake_sub = MagicMock()
    fake_sub.subscription_path = (
        lambda proj, name: f"projects/{proj}/subscriptions/{name}"
    )
    bus._subscriber = fake_sub
    ok = await bus._ensure_broadcast_subscription(
        Topics.Org.SETTINGS_CHANGED,
        f"core-api--{Topics.Org.SETTINGS_CHANGED}--abc123",
    )
    assert ok is True
    req = fake_sub.create_subscription.call_args.kwargs["request"]
    assert req["topic"] == f"projects/proj/topics/{Topics.Org.SETTINGS_CHANGED}"
    assert req["name"].endswith("--abc123")
    assert (
        req["expiration_policy"]["ttl"]["seconds"]
        == BROADCAST_SUBSCRIPTION_TTL_SECONDS
    )
    # Tracked so stop() can delete it.
    assert bus._broadcast_sub_paths == [req["name"]]


async def test_ensure_broadcast_subscription_already_exists_is_ok(
    bus: PubSubEventBus,
) -> None:
    from google.api_core import exceptions as gexc

    fake_sub = MagicMock()
    fake_sub.subscription_path = (
        lambda proj, name: f"projects/{proj}/subscriptions/{name}"
    )
    fake_sub.create_subscription = MagicMock(side_effect=gexc.AlreadyExists("exists"))
    bus._subscriber = fake_sub
    ok = await bus._ensure_broadcast_subscription(Topics.Org.SETTINGS_CHANGED, "sub-x")
    # Reuse a prior run's subscription (same _instance_id) rather than fail.
    assert ok is True
    assert len(bus._broadcast_sub_paths) == 1


async def test_ensure_broadcast_subscription_failure_degrades(
    bus: PubSubEventBus,
) -> None:
    # Missing IAM (pubsub.subscriptions.create) must NOT crash startup — the
    # process degrades to no fan-out (invalidation falls back to the cache TTL).
    fake_sub = MagicMock()
    fake_sub.subscription_path = (
        lambda proj, name: f"projects/{proj}/subscriptions/{name}"
    )
    fake_sub.create_subscription = MagicMock(
        side_effect=RuntimeError("permission denied")
    )
    bus._subscriber = fake_sub
    ok = await bus._ensure_broadcast_subscription(Topics.Org.SETTINGS_CHANGED, "sub-x")
    assert ok is False
    assert bus._broadcast_sub_paths == []


async def test_stop_deletes_broadcast_subscriptions(bus: PubSubEventBus) -> None:
    fake_sub = MagicMock()
    bus._subscriber = fake_sub
    bus._broadcast_sub_paths = ["projects/proj/subscriptions/core-api--x--abc"]
    await bus.stop()
    fake_sub.delete_subscription.assert_called_once()
    req = fake_sub.delete_subscription.call_args.kwargs["request"]
    assert req["subscription"] == "projects/proj/subscriptions/core-api--x--abc"
    assert bus._broadcast_sub_paths == []


async def test_release_broadcast_subscriptions_needs_nothing_else_torn_down(
    bus: PubSubEventBus,
) -> None:
    """The release must work on a fully live bus, with no teardown run first.

    This is the property the fix depends on. Cloud Run allows 10s between
    SIGTERM and SIGKILL; core-api's shutdown awaits three 5s-timeout flushes
    sequentially before it reaches the bus, so anything that can only run after
    them does not run at all on a busy shutdown. That is how 6,571 orphaned
    subscriptions accumulated in staging against a live instance count in the
    low tens, exhausting a project-wide cap shared with prod.

    So this asserts the release is callable in isolation — no stop(), no
    cancelled pull tasks, no closed clients — and that it is the delete alone.
    """
    fake_sub = MagicMock()
    bus._subscriber = fake_sub
    bus._started = True
    bus._broadcast_sub_paths = [
        "projects/proj/subscriptions/core-api--a--1",
        "projects/proj/subscriptions/core-api--b--2",
    ]

    await bus.release_broadcast_subscriptions()

    assert fake_sub.delete_subscription.call_count == 2
    assert bus._broadcast_sub_paths == []
    # It must not have torn the bus down as a side effect: stop() still has
    # work to do afterwards, and a release that closed the subscriber would
    # break the very ordering it exists to enable.
    fake_sub.close.assert_not_called()
    assert bus._started is True
    assert bus._stopped is False


async def test_released_subscription_exits_pull_loop_quietly(
    bus: PubSubEventBus,
) -> None:
    """A pull loop that outlives the release must not report a fault.

    The release runs ahead of the rest of teardown so it fits inside the 10s
    SIGTERM budget, which means a pull loop can still be mid-cycle when its
    subscription disappears. Without this the loop takes the permanent-error
    branch — ERROR log, ``_failed_subscriptions``, ``is_healthy`` false — on
    EVERY graceful shutdown, which is how a real alert gets tuned out.
    """
    from google.api_core import exceptions as gexc

    fake_sub = MagicMock()
    bus._subscriber = fake_sub
    bus._broadcast_sub_paths = ["projects/proj/subscriptions/core-api--x--abc"]
    await bus.release_broadcast_subscriptions()

    fake_sub.pull = MagicMock(side_effect=gexc.NotFound("gone"))
    bus._pull_executor = ThreadPoolExecutor(max_workers=1)
    await bus._pull_loop("core-api--x--abc", [])

    assert bus._failed_subscriptions == set()
    assert bus.is_healthy is True


async def test_unprovisioned_subscription_still_reports_loudly(
    bus: PubSubEventBus,
) -> None:
    """The suppression must be scoped to what we deleted, and nothing else.

    A NotFound on a subscription this process never released is a real
    configuration fault — never provisioned, or deleted by someone else — and
    must keep the loud halting path. A blanket suppression would trade one
    silent failure for another.
    """
    from google.api_core import exceptions as gexc

    fake_sub = MagicMock()
    bus._subscriber = fake_sub
    fake_sub.pull = MagicMock(side_effect=gexc.NotFound("never existed"))
    bus._pull_executor = ThreadPoolExecutor(max_workers=1)

    await bus._pull_loop("core-api--never-provisioned", [])

    assert "core-api--never-provisioned" in bus._failed_subscriptions
    assert bus.is_healthy is False


async def test_release_bounds_the_delete_rpc(bus: PubSubEventBus) -> None:
    """The delete must carry an explicit short timeout, not the SDK default.

    This method exists to finish inside Cloud Run's 10s SIGTERM budget. The
    SDK's generated default is 60s per call with a 60s retry deadline — six
    times the whole budget for one call — so inheriting it defeats the reason
    the method was split out: one slow Pub/Sub call and the process is killed
    mid-release, leaking exactly what this reclaims.

    A timed-out delete costs nothing that matters; ``expiration_policy`` still
    reaps the subscription. Bounding only trades a slow failure for a fast one.
    """
    from common.events.pubsub import BROADCAST_RELEASE_TIMEOUT_SECONDS

    fake_sub = MagicMock()
    bus._subscriber = fake_sub
    bus._broadcast_sub_paths = [
        "projects/proj/subscriptions/a--x--abc",
        "projects/proj/subscriptions/b--x--abc",
    ]

    await bus.release_broadcast_subscriptions()

    assert fake_sub.delete_subscription.call_count == 2
    for call in fake_sub.delete_subscription.call_args_list:
        assert call.kwargs["timeout"] == BROADCAST_RELEASE_TIMEOUT_SECONDS
    # Small enough that even a sequential worst case fits the budget.
    assert BROADCAST_RELEASE_TIMEOUT_SECONDS <= 5.0


async def test_release_survives_one_failing_delete(bus: PubSubEventBus) -> None:
    """One subscription failing must not strand the others.

    The deletes are fanned out concurrently; a single raise must neither abort
    its siblings nor escape into the shutdown path that called this.
    """
    fake_sub = MagicMock()
    bus._subscriber = fake_sub
    fake_sub.delete_subscription = MagicMock(
        side_effect=[RuntimeError("pubsub blip"), None]
    )
    bus._broadcast_sub_paths = [
        "projects/proj/subscriptions/a--x--abc",
        "projects/proj/subscriptions/b--x--abc",
    ]

    await bus.release_broadcast_subscriptions()

    assert fake_sub.delete_subscription.call_count == 2
    # Both are recorded as released regardless: the pull loops must exit
    # quietly either way, and the TTL reaps whatever the delete missed.
    assert bus._released_subscriptions == {"a--x--abc", "b--x--abc"}
    assert bus._broadcast_sub_paths == []


@pytest.mark.parametrize("exc_name", ["PermissionDenied", "InvalidArgument"])
async def test_released_subscription_still_reports_non_notfound_loudly(
    bus: PubSubEventBus,
    exc_name: str,
) -> None:
    """Releasing a name excuses a later NotFound on it, and nothing else.

    "We deleted this one ourselves" explains a subsequent NotFound. It does not
    explain a PermissionDenied — an IAM grant changed — or an InvalidArgument —
    we built a malformed request. Neither becomes benign because this process
    happened to release that name, and both are precisely what the halting path
    exists to surface.

    The original suppression sat inside the shared handler for all three, so a
    real IAM regression against a released name would exit the loop silently with
    ``is_healthy`` still true. ``test_unprovisioned_subscription_still_reports_loudly``
    does NOT cover this: it varies the *name* (never released) while holding the
    exception at NotFound, so it constrains only one of the two axes.
    """
    from google.api_core import exceptions as gexc

    fake_sub = MagicMock()
    bus._subscriber = fake_sub
    bus._broadcast_sub_paths = ["projects/proj/subscriptions/core-api--x--abc"]
    await bus.release_broadcast_subscriptions()
    assert "core-api--x--abc" in bus._released_subscriptions

    fake_sub.pull = MagicMock(side_effect=getattr(gexc, exc_name)("still a fault"))
    bus._pull_executor = ThreadPoolExecutor(max_workers=1)
    await bus._pull_loop("core-api--x--abc", [])

    assert "core-api--x--abc" in bus._failed_subscriptions
    assert bus.is_healthy is False


async def test_restart_clears_the_released_record(bus: PubSubEventBus) -> None:
    """Subscription names are deterministic per bus object, so a second start()
    recreates the same names. A leftover entry would silence a genuine NotFound
    on the next run — the suppression becoming the blind spot it exists to
    avoid."""
    fake_sub = MagicMock()
    bus._subscriber = fake_sub
    bus._broadcast_sub_paths = ["projects/proj/subscriptions/core-api--x--abc"]
    await bus.release_broadcast_subscriptions()
    assert "core-api--x--abc" in bus._released_subscriptions

    bus._started = False
    with contextlib.suppress(Exception):
        await bus.start()

    assert "core-api--x--abc" not in bus._released_subscriptions


async def test_release_broadcast_subscriptions_is_idempotent(
    bus: PubSubEventBus,
) -> None:
    """stop() still calls it, so it runs twice on every clean shutdown.

    A second delete of an already-deleted subscription would log a NotFound
    warning per topic on every graceful exit — noise that would train readers
    to ignore the one warning that matters.
    """
    fake_sub = MagicMock()
    bus._subscriber = fake_sub
    bus._broadcast_sub_paths = ["projects/proj/subscriptions/core-api--x--abc"]

    await bus.release_broadcast_subscriptions()
    await bus.release_broadcast_subscriptions()
    await bus.stop()

    fake_sub.delete_subscription.assert_called_once()


# ── broadcast slot identity ──────────────────────────────────────────
#
# The subscription name has to satisfy three properties AT ONCE. Each test
# below pins exactly one, because the properties trade against each other and a
# scheme that buys one by giving up another is the failure mode to guard:
# trading away "distinct between concurrent workers" turns a broadcast into a
# load-balanced queue, silently.
#
# These use real spawned processes, matching uvicorn's own supervisor
# (``multiprocessing.get_context("spawn")``). They have to: the property under
# test is that the KERNEL releases a claim when a process is SIGKILLed, and a
# mock cannot exhibit that.


def _slot_child(slot_dir: Path, queue: Any) -> None:
    """Claim a slot, report it, then block until killed — like a live worker."""
    queue.put(_claim_broadcast_slot(slot_dir, BROADCAST_MAX_SLOTS))
    time.sleep(300)


@pytest.fixture
def spawn_worker(tmp_path: Path) -> Any:
    """Spawn ``_slot_child`` processes into ``tmp_path`` and reap them all."""
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    started: list[Any] = []

    def _spawn() -> tuple[Any, str | None]:
        proc = ctx.Process(target=_slot_child, args=(tmp_path, queue))
        proc.start()
        started.append(proc)
        # Watch the child alongside the queue rather than just blocking on the
        # queue. A child that dies before reporting — an import failure under
        # spawn, an unwritable slot dir — would otherwise burn the entire
        # timeout per spawn before surfacing as an unhelpful Empty, turning an
        # already-failing run into a multi-minute one. A live child reports in
        # ~100ms; the generous ceiling is only there for a cold CI box.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                return proc, queue.get(timeout=0.1)
            except Empty:
                if proc.exitcode is not None:
                    # It may have reported and exited before we looked.
                    with contextlib.suppress(Empty):
                        return proc, queue.get(timeout=1)
                    raise AssertionError(
                        f"slot child exited {proc.exitcode} without reporting"
                    ) from None
        raise AssertionError("slot child never reported")

    yield _spawn
    for proc in started:
        if proc.is_alive():
            proc.kill()
        proc.join(timeout=10)


def test_broadcast_slot_is_stable_across_a_sigkilled_respawn(
    spawn_worker: Any,
) -> None:
    # PROPERTY 1 — stability. uvicorn SIGKILLs a worker that misses its 5s
    # healthcheck ping and spawns a replacement (``Multiprocess.
    # keep_subprocess_alive``). The replacement must re-derive its
    # predecessor's identity so ``_ensure_broadcast_subscription`` takes the
    # AlreadyExists path instead of minting a fresh subscription that orphans
    # the old one for a day. This is the property a random id fails, and it is
    # the entire leak.
    victim, first = spawn_worker()
    assert first is not None

    # Exactly what uvicorn does: SIGKILL, reap, spawn a brand-new process.
    # No shutdown hook runs — that is the point.
    victim.kill()
    victim.join(timeout=10)
    _replacement, second = spawn_worker()

    assert second == first, (
        f"respawned worker took identity {second!r}, predecessor held {first!r}; "
        "a changed identity orphans the predecessor's subscription for "
        f"{BROADCAST_SUBSCRIPTION_TTL_SECONDS}s"
    )


def test_broadcast_slot_is_distinct_between_concurrently_live_workers(
    tmp_path: Path, spawn_worker: Any
) -> None:
    # PROPERTY 2 — THE TRAP. Two live workers sharing one subscription does not
    # fail loudly: Pub/Sub load-balances between them and each sees roughly half
    # the events. For org.settings-changed that is a process serving stale
    # settings with nothing red anywhere. Worse than the leak, so stability must
    # never be bought at this property's expense.
    _first, ident_a = spawn_worker()
    _second, ident_b = spawn_worker()
    assert ident_a is not None and ident_b is not None
    assert ident_a != ident_b, (
        f"two concurrently-live workers both claimed {ident_a!r}; they would "
        "share one subscription and each receive only part of the broadcast"
    )

    # And the stronger form: a THIRD claimant, arriving while both incumbents
    # are still alive, must not be handed either of their identities. This is
    # the uvicorn ``restart_all`` (SIGHUP) shape, where the replacement is
    # started and waited on BEFORE the worker it replaces is terminated, so the
    # configured worker count is transiently exceeded.
    third = _claim_broadcast_slot(tmp_path, BROADCAST_MAX_SLOTS)
    assert third not in {ident_a, ident_b}, (
        f"overlap claimant got {third!r}, already held by a live worker"
    )


def test_broadcast_slot_is_distinct_across_instances(tmp_path: Path) -> None:
    # PROPERTY 3 — cross-instance distinctness. Every instance's slot 0 is a
    # different consumer. A bare slot index, or a bare Cloud Run instance id,
    # each satisfies two of the three properties and fails this one or
    # property 2; only the token+slot pair satisfies all three.
    # Two never-used directories stand in for two instances' filesystems, so
    # each of these is by construction the FIRST claim on its own instance —
    # the same slot on different instances, which is exactly the colliding
    # case. Asserted on the whole identity rather than on its parts: pinning
    # the slot component's shape here would couple this test to the id format
    # and make it fire for mutations that leave cross-instance distinctness
    # perfectly intact.
    ident_a = _claim_broadcast_slot(tmp_path / "instance-a", BROADCAST_MAX_SLOTS)
    ident_b = _claim_broadcast_slot(tmp_path / "instance-b", BROADCAST_MAX_SLOTS)
    assert ident_a is not None and ident_b is not None
    assert ident_a != ident_b, (
        f"the first slot on two separate instances both resolved to "
        f"{ident_a!r}; those two workers would share one subscription"
    )


def test_broadcast_slot_falls_back_rather_than_sharing_when_exhausted(
    tmp_path: Path,
) -> None:
    # The fallback DIRECTION, which must never be inverted. When no slot can be
    # proven free, the claim fails and the caller takes a random id — accepting
    # a bounded, quota-visible, day-lived leak. It must never wrap around and
    # reuse a held slot, which would be the silent collision.
    held = [_claim_broadcast_slot(tmp_path, 2) for _ in range(2)]
    assert None not in held and len(set(held)) == 2
    assert _claim_broadcast_slot(tmp_path, 2) is None


def test_identity_falls_back_to_a_random_id_when_no_slot_can_be_claimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An unclaimable slot must degrade to the pre-existing random-id behaviour,
    # not crash a service at boot. Delivery is unaffected; only the leak returns.
    monkeypatch.setattr("common.events.pubsub._slot_id", None)
    monkeypatch.setattr(
        "common.events.pubsub._claim_broadcast_slot", lambda *_a, **_k: None
    )
    assert len(_process_broadcast_slot_id()) == 12


def test_identity_is_stable_per_process_even_on_the_fallback_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The fallback must be memoised, not re-rolled per caller. An unmemoised
    # failure gives each bus in a process its OWN random id, which is exactly
    # the per-process property the memo exists to provide — two buses would
    # then create two subscriptions where one is wanted, and start()'s claim
    # that a start/stop/start cycle recreates the same names would be false.
    monkeypatch.setattr("common.events.pubsub._slot_id", None)
    monkeypatch.setattr(
        "common.events.pubsub._claim_broadcast_slot", lambda *_a, **_k: None
    )
    assert _process_broadcast_slot_id() == _process_broadcast_slot_id()


def test_claim_returns_none_rather_than_raising_on_an_unreadable_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Pins the WIDENING specifically, at the layer where it happens. Reading
    # the token file is the one filesystem call in this module that did not
    # degrade to None: every other one catches OSError, while this caught only
    # FileNotFoundError, so a PermissionError escaped the whole call chain.
    #
    # Deliberately asserted here rather than only through start(): the guard in
    # _process_broadcast_slot_id also catches this, so an end-to-end test stays
    # green with the narrow except restored and proves nothing about it. Two
    # isolation layers, so each needs its own test or neither is really pinned.
    real_read_text = Path.read_text

    def _unreadable(self: Path, *a: Any, **k: Any) -> str:
        if self.name == "instance":
            raise PermissionError(13, "Permission denied")
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", _unreadable)
    assert _claim_broadcast_slot(tmp_path, BROADCAST_MAX_SLOTS) is None


async def test_start_survives_an_unreadable_instance_token(
    bus: PubSubEventBus, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The module's stated contract is that ANY environment where the slot
    # cannot be claimed degrades to a random id. An unreadable token file is
    # such an environment, and it is reachable: the slot directory lives in a
    # shared tmpdir, so a file owned by another uid, or a directory where a
    # regular file is expected, both surface as OSError rather than as the
    # FileNotFoundError that the ordinary "first worker" case raises.
    #
    # This runs on the startup path, so the failure mode being pinned is a
    # service that will not boot — the exact opposite of the degradation the
    # docstring promises.
    #
    # A characterization test for the end-to-end contract, stated as such: two
    # layers now prevent this (the widened except in _read, and the guard in
    # _process_broadcast_slot_id), so it stays green if either one alone is
    # removed. The layer-specific tests above and below are what actually pin
    # them. Dressing this up as a regression guard for either is how the gap in
    # #991 got through.
    import types
    from unittest.mock import patch

    monkeypatch.setattr("common.events.pubsub._slot_id", None)
    monkeypatch.setattr("common.events.pubsub.BROADCAST_SLOT_DIR", tmp_path)

    real_read_text = Path.read_text

    def _unreadable(self: Path, *a: Any, **k: Any) -> str:
        if self.name == "instance":
            raise PermissionError(13, "Permission denied")
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", _unreadable)

    class FakeSubscriberClient:
        def subscription_path(self, proj: str, name: str) -> str:
            return f"projects/{proj}/subscriptions/{name}"

        def create_subscription(self, request: dict[str, Any]) -> None:
            return None

        def close(self) -> None:
            return None

    async def _h(_e: Event) -> None:
        return None

    bus.subscribe(Topics.Org.SETTINGS_CHANGED, _h, broadcast=True)
    with patch.object(
        PubSubEventBus,
        "_ensure_pubsub_sdk",
        return_value=types.SimpleNamespace(SubscriberClient=FakeSubscriberClient),
    ):
        await bus.start()

    # Booted, and with a usable identity rather than none at all.
    assert bus._started is True
    assert bus._broadcast_slot_id is not None
    assert len(bus._broadcast_slot_id) == 12  # the random fallback, not a slot
    bus._stopping = True  # keep teardown of the fake client quiet


def test_identity_survives_an_unexpected_error_in_the_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The totality guard lives in _process_broadcast_slot_id, not at the
    # start() call site, so it holds for every caller rather than for the one
    # that remembered to wrap it. Pinned with an exception the callees do not
    # anticipate at all, since the point is the unforeseen case.
    monkeypatch.setattr("common.events.pubsub._slot_id", None)

    def _boom(*_a: Any, **_k: Any) -> str:
        raise RuntimeError("something no callee anticipated")

    monkeypatch.setattr("common.events.pubsub._claim_broadcast_slot", _boom)
    assert len(_process_broadcast_slot_id()) == 12
