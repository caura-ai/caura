"""PubSubEventBus behaviour with the SDK mocked out.

Covers the parts that live in our code — envelope encoding, decode
robustness, ack/nack selection on handler outcome. The SDK-facing calls
are replaced by stand-ins so these tests run without GCP.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from common.events import Event, PubSubEventBus, Topics
from common.events.pubsub import BROADCAST_SUBSCRIPTION_TTL_SECONDS


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
