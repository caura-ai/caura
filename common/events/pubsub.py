"""GCP Pub/Sub event bus — used in SaaS deployments.

Publishers push JSON-encoded `Event` payloads to per-topic Pub/Sub
topics; subscribers pull messages from per-subscriber subscriptions and
dispatch to async handlers.

Topic + subscription provisioning is *not* done here — it's expected to
be managed via Terraform / gcloud at deploy time so subscription
configuration (ack deadlines, retry policies, dead-letter topics) lives
with infra. This class assumes the topics/subscriptions already exist.

Import is lazy so `common.events` can be imported in OSS standalone
installs that don't have `google-cloud-pubsub` installed.

**Cross-environment isolation.** When two environments (e.g. production
and a sandbox/staging) share one GCP project, they also share the topic
namespace — topic names are not env-scoped. Each env gets its *own*
subscription per topic, so Pub/Sub fans every published message out to
*both* environments' subscribers. The foreign-env subscriber then does
real work (a worker re-runs the embed/enrich provider call on the payload
content) before its tenant-scoped DB write no-ops, wasting spend and
emitting `target row missing` noise. To prevent this, the bus stamps each
message with a `source_env` attribute (see ``SOURCE_ENV_ATTRIBUTE``) and
``_pull_loop`` ack-drops any message whose `source_env` differs from this
process's ``env`` *before* decode/dispatch. The guard is a no-op when
``env`` is unset or the attribute is absent (backward-compatible with
publishers that predate the attribute), so it is safe to roll out in any
order. A follow-up moves the same drop server-side via a subscription
`filter` on the attribute.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import json
import logging
from collections import defaultdict
from typing import Any

from pydantic import ValidationError

from common.events.base import Event, EventBus, EventHandler

logger = logging.getLogger(__name__)

# Pub/Sub message attribute carrying the publishing environment's identity
# (e.g. "production" / "sandbox"). Stamped on every publish when the bus is
# constructed with an ``env`` and used by ``_pull_loop`` to drop foreign-env
# messages. It is a Pub/Sub *attribute* (not a field in the JSON envelope) so
# a future subscription ``filter`` can drop foreign messages server-side —
# filters can only match attributes, never the message body. See the
# cross-environment leakage note in the module docstring.
SOURCE_ENV_ATTRIBUTE = "source_env"


class PubSubEventBus(EventBus):
    """Pub/Sub-backed bus.

    Construction args:
        project_id: GCP project that owns the topics/subscriptions.
        subscription_prefix: Applied to the *handler side* when we call
            `subscribe(topic, handler)`. Subscriptions are named
            ``f"{subscription_prefix}--{topic}"`` so a single topic can
            have multiple distinct consumers (each service gets its own
            subscription_prefix).

    The handler side spawns one async pull task per subscription when
    `start()` is called. Each pull task receives a message, runs the
    handler, and ack/nacks based on the outcome. Pub/Sub handles redelivery
    on nack.

    **At-least-once delivery**: handlers registered against this bus
    *must* be idempotent. Pub/Sub redelivers on ack failure and on
    handler exceptions; a handler that already ran can be invoked again
    for the same event. Use `event.event_id` as a natural dedup key.
    """

    def __init__(
        self,
        project_id: str,
        subscription_prefix: str,
        *,
        # Identity of the publishing environment ("production" /
        # "sandbox" / ...). Stamped onto every published message as the
        # ``SOURCE_ENV_ATTRIBUTE`` Pub/Sub attribute and used by
        # ``_pull_loop`` to drop foreign-env messages that fanned out
        # from a sibling environment sharing this project's topics. When
        # None the cross-env guard is disabled (no stamping, no
        # filtering) — preserves the pre-guard behaviour for single-env
        # deployments and in-process tests.
        env: str | None = None,
        # Batch size per pull. Caps ``workers × max_messages``
        # concurrent embed calls across the deployed pool, so 25
        # balances drain throughput against the OpenAI per-org
        # rate limit and the blast radius of a single wedged
        # dispatch cycle.
        max_messages: int = 25,
        pull_timeout: float = 20.0,
        error_backoff: float = 5.0,
        publish_concurrency: int = 32,
    ) -> None:
        # No SDK import at construction: the factory can return this
        # instance even in environments where google-cloud-pubsub isn't
        # installed. The ImportError surfaces on first `publish` /
        # `start` — see `_ensure_pubsub_sdk` below.
        self._project_id = project_id
        self._subscription_prefix = subscription_prefix
        # Normalise so a stray trailing space in the env var can't make a
        # publisher's stamp mismatch a subscriber's comparison. Empty
        # string collapses to None so it behaves identically to "unset".
        self._env = env.strip() if env and env.strip() else None
        self._max_messages = max_messages
        self._pull_timeout = pull_timeout
        self._error_backoff = error_backoff
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)
        self._publisher: Any = None
        self._subscriber: Any = None
        self._pull_tasks: list[asyncio.Task[None]] = []
        self._stopping = False
        self._publish_concurrency = publish_concurrency
        # One-shot flag so the "subscribe without start()" warning fires
        # on the first publish only — otherwise we'd spam the log under
        # sustained traffic. Reset in stop() so restarts reuse the same
        # one-shot.
        self._warned_missing_start = False
        # Bounded executors for blocking SDK calls — lazy-init so stop()
        # can shut them down and a subsequent start()/publish() gets a
        # fresh one. asyncio's default executor is effectively unbounded
        # per event loop; capping keeps a publish burst's blast radius
        # predictable. Separate pools per role (publish vs pull) so a
        # publish saturation can't starve message consumption and vice
        # versa, and so stop() can close the subscriber first (waking
        # blocked pull threads immediately via the gRPC channel error)
        # then drain the pool, without the publish pool stalling that
        # teardown sequence.
        self._publish_executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._pull_executor: concurrent.futures.ThreadPoolExecutor | None = None
        # Subscriptions whose pull loop halted on a permanent error
        # (NotFound/PermissionDenied/InvalidArgument). Exposed via
        # `is_healthy` so a health endpoint can mark the service unhealthy
        # when consumption has stopped without crashing the process.
        self._failed_subscriptions: set[str] = set()
        # Set at the top of stop() before any executor teardown begins,
        # so a concurrent publish() that raced through can't lazy-init
        # a fresh executor that stop() never joins. Reset at the end of
        # stop() so the bus can be restarted cleanly.
        self._stopped: bool = False
        # Monotonic counter incremented at the top of every ``stop()``.
        # Lets ``start()`` detect a stop() that ran to FULL completion
        # while it was suspended on an executor await — a clean teardown
        # resets both ``_stopped`` and ``_stopping`` to False in stop()'s
        # finally, so a flag-only check would miss the race. ``start()``
        # captures this counter before the shielded await and bails if
        # it changed afterwards.
        self._stop_generation: int = 0
        # Tracks whether start() has been awaited. Used by publish() to
        # warn when subscribers are registered but start() was never
        # called. Cleaner than inferring from `_pull_tasks`, which can
        # also be empty after permanent pull errors or a clean stop().
        self._started: bool = False
        # Strong references for fire-and-forget background tasks
        # (TOCTOU loser-close, post-stop candidate-close, cancelled-
        # construction close). Without this set, ``asyncio.create_task``
        # only stores its task in ``asyncio._all_tasks`` (a WeakSet)
        # and the GC can collect it mid-await — Python docs explicitly
        # warn against bare ``create_task(coro)``. The done-callback
        # auto-discards on completion so the set stays bounded.
        self._background_tasks: set[asyncio.Task[Any]] = set()

    def _spawn_background_task(self, coro: Any) -> asyncio.Task[Any]:
        """Schedule a fire-and-forget task; see ``_background_tasks`` for why."""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    # ── publisher ──────────────────────────────────────────────────

    @staticmethod
    def _ensure_pubsub_sdk() -> Any:
        try:
            from google.cloud import pubsub_v1  # type: ignore[import-untyped]

            return pubsub_v1
        except ImportError as exc:
            raise RuntimeError(
                "PubSubEventBus requires `google-cloud-pubsub`. "
                "Install with: pip install 'google-cloud-pubsub>=2.23,<3'"
            ) from exc

    async def _ensure_publisher(self) -> Any:
        # Off-loop construction so a Workload Identity credential
        # refresh inside the SDK doesn't pin the event loop on the
        # first publish. TOCTOU-safe: if a concurrent caller
        # populates ``_publisher`` while this one is awaiting the
        # executor, the losing candidate is explicitly closed —
        # ``PublisherClient`` wraps a gRPC channel and a background
        # batch-flush thread, so leaving it for GC is non-deterministic
        # (no documented ``__del__`` contract).
        # Fast-path stop guard — symmetric with ``_get_publish_executor``.
        # Refuse to even start construction work when the bus is
        # already stopped (or stopping); the post-await guard below
        # catches the same conditions when ``stop()`` races during
        # the shielded await, but a synchronous bail-out before the
        # SDK import avoids unnecessary executor + gRPC churn.
        if self._stopped or self._stopping:
            raise RuntimeError("PubSubEventBus is stopped")
        if self._publisher is not None:
            return self._publisher
        pubsub_v1 = self._ensure_pubsub_sdk()
        loop = asyncio.get_running_loop()
        # Generation snapshot so we can detect a ``stop()`` that ran to
        # full completion during the executor await — clean teardown
        # resets ``_stopped`` to False, so a flag-only check would miss
        # the race. Without this, a fresh client gets installed into a
        # bus that ``stop()`` already walked past, orphaning both the
        # publisher and the executor it triggers in ``_get_publish_executor``.
        stop_generation_before = self._stop_generation
        # ``asyncio.shield`` so a task cancellation here doesn't strand
        # the in-flight ``PublisherClient()`` inside the executor — the
        # SDK call doesn't honour cancellation, so without the shield
        # the future completes anyway but the caller never captures it,
        # leaking the gRPC channel + flush thread.
        ctor_fut = loop.run_in_executor(None, pubsub_v1.PublisherClient)
        try:
            candidate = await asyncio.shield(ctor_fut)
        except asyncio.CancelledError:
            # Cancelled mid-construction. Spawn a background closer for
            # the still-pending future so the leaked client gets
            # released; re-raise so the caller's cancellation
            # propagates.
            async def _close_pending() -> None:
                try:
                    client = await ctor_fut
                    await loop.run_in_executor(None, client.close)
                except BaseException:
                    # ``BaseException`` (not ``Exception``) — this is a
                    # fire-and-forget background task; if the event loop
                    # cancels it during shutdown, the resulting
                    # ``CancelledError`` would otherwise surface as
                    # "Task exception was never retrieved" log noise.
                    logger.debug(
                        "pubsub: cancelled-publisher close failed", exc_info=True
                    )

            self._spawn_background_task(_close_pending())
            raise
        # Post-stop guard: if a concurrent ``stop()`` ran to completion
        # during the await, installing the candidate would leak — the
        # bus is "stopped" again only because stop()'s finally reset
        # the flags after its cleanup. Close the candidate in the
        # background and bail; ``publish()`` will get a RuntimeError
        # which is the right shape for "bus was stopped".
        if (
            self._stopped
            or self._stopping
            or self._stop_generation != stop_generation_before
        ):

            async def _close_post_stop() -> None:
                try:
                    await loop.run_in_executor(None, candidate.close)
                except BaseException:
                    logger.debug(
                        "pubsub: post-stop candidate close failed", exc_info=True
                    )

            self._spawn_background_task(_close_post_stop())
            raise RuntimeError(
                "PubSubEventBus was stopped during publisher construction"
            )
        if self._publisher is None:
            self._publisher = candidate
            return self._publisher
        if candidate is not self._publisher:
            # Fire-and-forget close of the TOCTOU loser. Awaiting here
            # would let an outer ``task.cancel()`` bleed into a pure
            # cleanup branch; catching ``CancelledError`` to suppress
            # it would clear the task's cancel-request (Py 3.9+) and
            # let ``publish()`` continue to send the message even
            # though the caller cancelled — a real bug. Schedule the
            # close, return the cached publisher, and let any outer
            # cancel propagate normally. (No interleaving risk between
            # the create_task and the return: ``asyncio.create_task``
            # is synchronous and doesn't yield.)
            async def _close_loser() -> None:
                try:
                    await loop.run_in_executor(None, candidate.close)
                except BaseException:
                    # ``BaseException`` so a shutdown-time cancellation
                    # of this background task doesn't surface as "Task
                    # exception was never retrieved".
                    logger.debug(
                        "pubsub: discarded duplicate PublisherClient.close() failed",
                        exc_info=True,
                    )

            self._spawn_background_task(_close_loser())
        return self._publisher

    @property
    def is_healthy(self) -> bool:
        """True when the bus is in a state where it can still deliver
        events end-to-end.

        False in three cases:

        1. ``stop()`` is in progress. The ``_stopped`` flag flips True
           at the top of ``stop()`` (before any await point) and resets
           False only at the very end — while True the bus has already
           cancelled its pull tasks and is actively tearing down.
           Checking this first means the graceful-shutdown window
           starts when ``stop()`` is *called*, not when it *completes*.
        2. Handlers were registered via ``subscribe()`` but ``start()``
           was never awaited — the pull loops don't exist, so every
           inbound event is silently dropped. ``_failed_subscriptions``
           is empty in this case (the loops never ran) so we rely on
           ``_started`` to distinguish "deliberately publisher-only"
           (no handlers, healthy) from "forgot to call start()" (has
           handlers but unstarted, unhealthy). This case also carries
           the post-``stop()`` state: once ``_stopped`` resets to
           False, a bus with handlers still reports unhealthy until
           ``start()`` is (re-)called.
        3. Any pull loop has halted on a permanent error (subscription
           missing, SA permission revoked) — recorded in
           ``_failed_subscriptions`` by ``_pull_loop``.

        All three windows report unhealthy intentionally — the pod is
        not consuming events during them, so it should not claim it is.

        Surface this on the service's `/health` endpoint so a
        misconfigured pod is marked unhealthy instead of silently
        dropping events while the HTTP surface stays green.
        """
        if self._stopped:
            return False
        if self._handlers and not self._started:
            return False
        return not self._failed_subscriptions

    def _get_publish_executor(self) -> concurrent.futures.ThreadPoolExecutor:
        # Refuse to spin up a fresh executor during stop()'s teardown
        # window — otherwise a concurrent publish() could create a pool
        # that stop() has already walked past and will never join.
        if self._stopped:
            raise RuntimeError("PubSubEventBus is stopped")
        if self._publish_executor is None:
            self._publish_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=self._publish_concurrency,
                thread_name_prefix="pubsub-publish",
            )
        return self._publish_executor

    async def publish(self, topic: str, event: Event) -> None:
        # Misconfiguration signal: handlers were registered but start()
        # was never called, so no pull loop exists. Publishing still
        # works (separate publisher/subscriber role) but a single-process
        # deploy that forgot start() will silently drop inbound events.
        # Warn once so the first publish logs it.
        #
        # Thread-safety of the one-shot flag: asyncio is single-threaded
        # on the event loop, but ``run_in_executor`` below yields control.
        # Two concurrent publish() calls could both evaluate the guard
        # before either wrote True — flip the flag BEFORE logging so the
        # second call observes the set flag and skips the log.
        if self._handlers and not self._started and not self._warned_missing_start:
            self._warned_missing_start = True
            logger.warning(
                "PubSubEventBus.publish() called with subscribers registered "
                "but start() was never awaited — this bus will never receive "
                "events. Call `await bus.start()` at service startup."
            )
        publisher = await self._ensure_publisher()
        topic_path = publisher.topic_path(self._project_id, topic)
        payload = event.model_dump_json().encode("utf-8")
        # Fire the publish into the client's internal batch queue and
        # return — do NOT block on the returned Future. .result(timeout=30)
        # would tie up one of the `publish_concurrency` executor threads
        # for up to 30 s on a slow / unavailable Pub/Sub, letting a
        # sustained outage wedge every caller of publish_audit_event
        # (admin-API request handlers await this, so queued publish
        # threads translate directly to queued requests).
        #
        # At-least-once is preserved: stop()'s shutdown(wait=True) on the
        # publish executor drains any in-flight batches before the
        # process exits. The tradeoff is that we no longer surface
        # per-message publish errors to the caller — publisher-side
        # failures land in the SDK's background-thread log instead.
        # For a fire-and-forget audit path that is the right shape.
        # Stamp the publishing environment so sibling environments that
        # share this project's topics can drop our fan-out copies (see
        # ``_pull_loop`` and the module docstring). Passed as a Pub/Sub
        # *attribute* (kwarg to ``publish``) rather than folded into the
        # JSON body so a subscription ``filter`` can later match it
        # server-side. Omitted entirely when ``env`` is unset so the wire
        # format is unchanged for single-env deployments.
        attributes = {SOURCE_ENV_ATTRIBUTE: self._env} if self._env else {}
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            self._get_publish_executor(),
            functools.partial(publisher.publish, topic_path, payload, **attributes),
        )

    # ── subscriber ─────────────────────────────────────────────────

    def subscribe(self, topic: str, handler: EventHandler) -> None:
        # Late subscribes silently orphan the handler — pull tasks are
        # only spawned during start(). Fail loudly so the bug surfaces
        # at wire-up rather than appearing as "events aren't arriving".
        # Check ``_started`` (same authoritative signal start() uses) —
        # ``_pull_tasks`` is empty for publisher-only buses, which would
        # let a late subscribe() on a started publisher silently orphan.
        if self._started:
            raise RuntimeError(
                "PubSubEventBus.subscribe() must be called before start(); "
                "the pull loop for this topic won't be created otherwise."
            )
        self._handlers[topic].append(handler)

    async def start(self) -> None:
        # Idempotent guard — a second call would leak the old
        # SubscriberClient's gRPC channel and spawn duplicate pull tasks
        # that double-process every message. Check ``_started`` rather
        # than ``_pull_tasks``: a publisher-only bus (no subscribers)
        # has empty ``_pull_tasks`` even after a successful start(), so
        # the old check silently re-ran the sequence instead of warning.
        if self._started:
            logger.warning("PubSubEventBus.start() called more than once; ignoring")
            return
        pubsub_v1 = self._ensure_pubsub_sdk()
        topic_count = len(self._handlers)
        # SubscriberClient construction can block the event loop for
        # hundreds of ms when the Pub/Sub SDK triggers Workload Identity
        # credential refresh (metadata-server round trip) on its first
        # auth-backed call. Offload to the default executor so the loop
        # stays responsive during service boot.
        #
        # PublisherClient is NOT constructed here: a subscriber-only
        # service that never calls publish() shouldn't hold an unused
        # gRPC channel. publish() handles the off-loop construction
        # lazily on the first call (see ``_ensure_publisher``).
        if topic_count > 0:
            loop = asyncio.get_running_loop()
            # Snapshot the stop generation BEFORE the shielded await.
            # A clean ``stop()`` resets both ``_stopped`` and
            # ``_stopping`` to False in its finally, so a flag-only
            # check after the await would miss the race when a complete
            # stop() ran while we were suspended. Comparing the
            # generation catches it.
            stop_generation_before = self._stop_generation
            # ``asyncio.shield`` so a task cancellation during the
            # SubscriberClient construction (e.g. uvicorn lifespan
            # cancelling start()) doesn't strand the in-flight client
            # inside the executor without a close().
            ctor_fut = loop.run_in_executor(None, pubsub_v1.SubscriberClient)
            try:
                self._subscriber = await asyncio.shield(ctor_fut)
            except asyncio.CancelledError:

                async def _close_pending() -> None:
                    try:
                        client = await ctor_fut
                        await loop.run_in_executor(None, client.close)
                    except BaseException:
                        # ``BaseException`` (not ``Exception``) — fire-
                        # and-forget background task; a shutdown-time
                        # cancel would otherwise show up as a "Task
                        # exception was never retrieved" warning.
                        logger.debug(
                            "pubsub: cancelled-subscriber close failed",
                            exc_info=True,
                        )

                self._spawn_background_task(_close_pending())
                raise
            # The shielded await above is the first yield ever introduced
            # into ``start()`` — a concurrent ``stop()`` (e.g. SIGTERM
            # during boot under Cloud Run's 10s startup-probe window)
            # can run to completion while we're suspended. The flag
            # check catches an in-progress stop(); the generation check
            # catches a stop() that fully completed (and reset both
            # flags) during the await. Either signal aborts so we don't
            # install pull tasks into a bus that stop() already walked
            # past.
            if (
                self._stopping
                or self._stopped
                or self._stop_generation != stop_generation_before
            ):
                sub = self._subscriber
                self._subscriber = None
                try:
                    await loop.run_in_executor(None, sub.close)
                except Exception:
                    logger.debug(
                        "pubsub: start() aborted by concurrent stop(); "
                        "subscriber.close() failed",
                        exc_info=True,
                    )
                # Raise rather than silently return — a lifespan handler
                # awaiting ``bus.start()`` would otherwise see a normal
                # return and assume the bus is operational. ``is_healthy``
                # eventually flips False once the first probe runs (the
                # ``handlers and not _started`` branch), but the gap
                # between this return and that probe is a silently
                # degraded window. Make the failure visible at startup.
                raise RuntimeError(
                    "PubSubEventBus.start() aborted: stop() ran "
                    "concurrently during SubscriberClient construction"
                )
            self._pull_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=max(4, 2 * topic_count),
                thread_name_prefix="pubsub-pull",
            )
            for topic, handlers in self._handlers.items():
                sub_name = f"{self._subscription_prefix}--{topic}"
                task = asyncio.create_task(self._pull_loop(sub_name, handlers))
                self._pull_tasks.append(task)
        self._started = True

    def _foreign_source_env(self, message: Any) -> str | None:
        """Return the message's ``source_env`` when it was published by a
        *different* environment than this bus — i.e. the message should be
        dropped — otherwise ``None`` (process it normally).

        Returns ``None`` (= process) in every backward-compatible case:

        - this bus has no ``env`` configured, so the guard is disabled;
        - the message carries no ``source_env`` attribute (the publisher
          predates the attribute, or it came from outside this bus —
          e.g. a Google-originated push), so we can't prove it's foreign;
        - the attribute equals this bus's ``env``.

        Only a *present* attribute that *differs* from ``self._env`` is
        treated as foreign. This keeps the guard safe to deploy in any
        order: until every publisher stamps the attribute, unstamped
        messages keep their pre-guard fan-out behaviour rather than being
        silently dropped.
        """
        if self._env is None:
            return None
        attributes = getattr(message, "attributes", None) or {}
        source_env = attributes.get(SOURCE_ENV_ATTRIBUTE)
        if not source_env or source_env == self._env:
            return None
        return source_env

    async def _pull_loop(
        self, subscription_name: str, handlers: list[EventHandler]
    ) -> None:
        from google.api_core import exceptions as gexc  # type: ignore[import-untyped]

        # Stopped check goes first — symmetric with `_get_publish_executor`.
        # A pull loop that got scheduled right before stop() flipped the
        # flag shouldn't start a new pull cycle.
        if self._stopped:
            raise RuntimeError("PubSubEventBus is stopped")
        # Capture the subscriber reference once so stop() setting it to
        # None can't race with the in-flight pull/ack calls below.
        subscriber = self._subscriber
        if subscriber is None:
            # `start()` should always set `_subscriber` before spawning
            # pull tasks — an explicit raise documents the invariant and
            # survives `-O` (bare `assert` gets stripped there).
            raise RuntimeError(
                "PubSubEventBus._pull_loop invoked without a subscriber — "
                "this is a programming error in start()."
            )
        sub_path = subscriber.subscription_path(self._project_id, subscription_name)
        loop = asyncio.get_running_loop()
        # Dedicated executor (not asyncio's default None) so stop() can
        # drain pull/ack threads before closing `subscriber` — otherwise
        # a thread still blocked in subscriber.pull(timeout=...) wakes
        # up holding a reference to a closed gRPC channel. start()
        # creates this before spawning pull tasks, so it must be set
        # by the time we run here.
        pull_executor = self._pull_executor
        if pull_executor is None:
            raise RuntimeError(
                "_pull_loop invoked with no pull executor — this is a "
                "programming error in start()."
            )

        while not self._stopping:
            try:
                response = await loop.run_in_executor(
                    pull_executor,
                    functools.partial(
                        subscriber.pull,
                        request={
                            "subscription": sub_path,
                            "max_messages": self._max_messages,
                        },
                        timeout=self._pull_timeout,
                    ),
                )
                ack_ids: list[str] = []
                nack_ids: list[str] = []
                for received in response.received_messages:
                    foreign_env = self._foreign_source_env(received.message)
                    if foreign_env is not None:
                        # Fan-out copy from a sibling environment sharing
                        # this project's topics. Ack-drop *before* decode
                        # and dispatch — running the handler would re-run a
                        # provider (embed/enrich) call on the payload and
                        # then no-op against a tenant row that doesn't exist
                        # in this env's DB, wasting spend and emitting
                        # `target row missing` noise. Ack (not nack) so it
                        # isn't redelivered; the owning env has its own
                        # subscription and processes its own copy.
                        ack_ids.append(received.ack_id)
                        logger.info(
                            "event-bus: dropping foreign-env message",
                            extra={
                                "subscription": subscription_name,
                                "source_env": foreign_env,
                                "env": self._env,
                            },
                        )
                        continue
                    event = self._decode(received.message.data)
                    if event is None:
                        # Malformed message — ack so we don't redeliver
                        # forever, log for alerting.
                        ack_ids.append(received.ack_id)
                        continue
                    success = await self._dispatch_all(handlers, event)
                    (ack_ids if success else nack_ids).append(received.ack_id)

                # Ack/nack must stay inside this try: a transient network
                # error during acknowledge would otherwise escape, kill
                # the task, and silently stop consumption forever. On
                # failure Pub/Sub redelivers via ack-deadline expiry.
                if ack_ids:
                    ack_request = {"subscription": sub_path, "ack_ids": ack_ids}
                    await loop.run_in_executor(
                        pull_executor,
                        functools.partial(subscriber.acknowledge, request=ack_request),
                    )
                if nack_ids:
                    nack_request = {
                        "subscription": sub_path,
                        "ack_ids": nack_ids,
                        "ack_deadline_seconds": 0,
                    }
                    await loop.run_in_executor(
                        pull_executor,
                        functools.partial(
                            subscriber.modify_ack_deadline, request=nack_request
                        ),
                    )
            except gexc.DeadlineExceeded:
                # No messages in the pull window; loop back and try again.
                continue
            except (
                gexc.NotFound,
                gexc.PermissionDenied,
                gexc.InvalidArgument,
            ):
                # Permanent configuration errors: subscription doesn't
                # exist, service account lacks permission, or the
                # request shape is wrong. Retrying spins the log forever
                # without ever succeeding — halt the loop and let ops
                # see the error once, loud. Record the subscription so
                # `is_healthy` can surface it to a health endpoint.
                logger.error(
                    "pubsub permanent error; halting pull loop — check "
                    "subscription provisioning and service-account permissions",
                    extra={"subscription": subscription_name},
                    exc_info=True,
                )
                self._failed_subscriptions.add(subscription_name)
                return
            except asyncio.CancelledError:
                # ``except Exception:`` below would miss ``CancelledError``
                # (BaseException subclass on Py 3.8+). The graceful
                # shutdown path lands here when ``stop()`` cancels the
                # pull task — re-raise so the task unwinds cleanly and
                # ``stop()``'s ``gather(return_exceptions=True)`` captures
                # it.
                #
                # Non-shutdown ``CancelledError`` (a handler awaiting a
                # separately-cancelled task / future — a programming
                # bug) is treated as a permanent halt rather than a
                # nack-and-continue: marking ``_failed_subscriptions``
                # flips ``is_healthy`` False so the load balancer drains
                # this pod, the event re-delivers via Pub/Sub
                # ack-deadline expiry to a healthy replica, and the
                # buggy code path can be diagnosed via the readiness
                # probe failure rather than as silent message loss.
                # Recovery requires a service restart — same shape as
                # the ``NotFound``/``PermissionDenied``/``InvalidArgument``
                # branch above, since both classes of error are unsafe
                # to retry without operator intervention.
                if not self._stopping:
                    self._failed_subscriptions.add(subscription_name)
                raise
            except Exception:
                # During shutdown we deliberately close the subscriber
                # first so pull threads wake immediately; the resulting
                # gRPC error is expected, not something to log or sleep
                # on. Short-circuit so `stop()` doesn't wait 5s per
                # subscription for nothing.
                if self._stopping:
                    return
                logger.exception(
                    "pubsub pull/ack error; backing off",
                    extra={"subscription": subscription_name},
                )
                await asyncio.sleep(self._error_backoff)
                continue

    @staticmethod
    def _decode(data: bytes) -> Event | None:
        # Pydantic shifted `ValidationError`'s base across v2 minors
        # (v1 + current ≥2.4 inherit from ValueError, 2.0-2.3 did not),
        # so we catch both explicitly rather than assume either. A
        # schema-invalid-but-valid-JSON message must drop here; if it
        # escaped to `_pull_loop`'s outer handler the loop would back
        # off without acking and Pub/Sub would redeliver forever.
        # `json.JSONDecodeError` already inherits from ValueError since
        # Python 3.5, so ValueError covers it — listed explicitly would
        # be redundant.
        try:
            parsed: dict[str, Any] = json.loads(data.decode("utf-8"))
            return Event.model_validate(parsed)
        except (ValueError, ValidationError):
            logger.exception("failed to decode pubsub message; acking to drop")
            return None

    async def _dispatch_all(self, handlers: list[EventHandler], event: Event) -> bool:
        """Run every handler concurrently and ack/nack on aggregate outcome.

        Concurrent (via `asyncio.gather`) matches `InProcessEventBus`,
        which spawns each handler as its own task — code validated
        against the in-process bus keeps identical semantics here, and
        a slow handler can't serialise the rest. Nack (return False)
        when any handler raised, so Pub/Sub redelivers. Handlers must
        be idempotent — a handler that already succeeded will see the
        redelivered event again.
        """
        results = await asyncio.gather(
            *(handler(event) for handler in handlers),
            return_exceptions=True,
        )
        # CancelledError inherits from BaseException (not Exception)
        # and ``return_exceptions=True`` above converts it to a
        # returned value rather than re-raising — so an unchecked
        # cancellation slips past the ``isinstance(result, Exception)``
        # branch below and silently acks the message. Defer the raise
        # until the loop has logged every Exception result; raising
        # eagerly on the first cancellation would silently drop later
        # handler-failure logs from a mixed batch.
        cancelled: asyncio.CancelledError | None = None
        all_ok = True
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                # Preserve the first cancellation rather than overwriting
                # — a mixed batch's later cancellations would otherwise
                # silently drop. Functionally either re-raise unwinds the
                # loop correctly, but the first carries the most context.
                if cancelled is None:
                    cancelled = result
                continue
            if isinstance(result, Exception):
                logger.exception(
                    "pubsub handler raised; nacking for redelivery",
                    exc_info=result,
                    extra={
                        "event_type": event.event_type,
                        "event_id": str(event.event_id),
                    },
                )
                all_ok = False
        if cancelled is not None:
            raise cancelled
        return all_ok

    async def stop(self) -> None:
        # Flip `_stopped` first so `_get_publish_executor` / `_pull_loop`
        # refuse to spin up new resources after this point — closes the
        # race where a concurrent publish() would create an executor
        # that stop() has already walked past and will never join.
        self._stopped = True
        self._stopping = True
        # Bump generation BEFORE the teardown body so a concurrent
        # ``start()`` suspended on its shielded SubscriberClient await
        # sees a non-zero delta even when this stop() runs to clean
        # completion (the finally below resets both flags to False).
        self._stop_generation += 1
        loop = asyncio.get_running_loop()
        # `teardown_complete` gates the lifecycle-flag reset in the
        # finally. It flips True only at the very end of the try body,
        # after every close/shutdown has run to completion. Per-step
        # Exception guards below catch ordinary close failures and
        # continue, so those still let us reach the end — the gate
        # matters for BaseException escapes (CancelledError from a
        # uvicorn shutdown-deadline, KeyboardInterrupt, etc.). In that
        # case some `_pull_executor` / `_publish_executor` / `_publisher`
        # attributes may still reference live SDK threads whose join
        # never ran, and leaving `_started=True` blocks a re-entry from
        # overwriting those references and orphaning the threads
        # permanently.
        # `close_failure_count` tallies per-step failures within this
        # invocation. A non-zero count on a successful teardown still
        # means gRPC channels / threads leaked SDK-side — we log a
        # warning so repeated start/stop cycles with close failures
        # surface as a visible pattern rather than silently accumulating
        # file descriptors or thread-pool entries. Local (not instance)
        # so a concurrent stop() can't clobber the other's count, and
        # nothing outside stop() has a legitimate read interest.
        teardown_complete = False
        close_failure_count = 0
        # `_pull_tasks.clear()` is deliberately deferred until every
        # blocking cleanup step below finishes. ``_started`` is flipped
        # to False only at the end of stop() for the same reason — a
        # concurrent ``start()`` must not pass its ``if self._started:``
        # idempotency guard and create a fresh SubscriberClient that
        # the in-flight stop() would then close out from under the caller.
        try:
            # Close the subscriber BEFORE cancelling/awaiting the pull
            # tasks. Pull threads are blocked inside a synchronous
            # `subscriber.pull(timeout=pull_timeout)` — asyncio
            # cancellation removes the task reference but can't interrupt
            # the thread, so `gather()` below waits up to ``pull_timeout``
            # per task for the SDK call to return on its own. Closing the
            # subscriber first errors the gRPC channel, wakes every
            # blocked thread immediately, and lets the pull tasks finish
            # near-instantly — critical under Cloud Run's 10 s SIGTERM
            # window or `stop()` gets SIGKILL'd and in-flight ack calls
            # never complete.
            # Offloaded to a thread because SDK .close() waits on
            # background gRPC channels to drain — keeps the event loop
            # responsive for other tasks' cancellations during lifespan
            # shutdown.
            if self._subscriber is not None:
                sub = self._subscriber
                self._subscriber = None
                try:
                    await loop.run_in_executor(None, sub.close)
                except Exception:
                    close_failure_count += 1
                    logger.exception(
                        "pubsub subscriber.close() failed; continuing teardown"
                    )
            for t in self._pull_tasks:
                t.cancel()
            # `return_exceptions=True` captures task-level exceptions
            # (including CancelledError from the t.cancel() above) as
            # result values rather than raising — so this await is
            # already safe against handler/loop failures. The only
            # remaining way it raises is external cancellation of
            # stop() itself (e.g. uvicorn's shutdown deadline firing),
            # and that *should* propagate so the caller learns the
            # deadline was missed.
            await asyncio.gather(*self._pull_tasks, return_exceptions=True)
            if self._pull_executor is not None:
                pull_exec = self._pull_executor
                self._pull_executor = None
                try:
                    await loop.run_in_executor(None, pull_exec.shutdown, True)
                except Exception:
                    close_failure_count += 1
                    logger.exception(
                        "pubsub pull executor shutdown failed; continuing teardown"
                    )
            if self._publish_executor is not None:
                pub_exec = self._publish_executor
                self._publish_executor = None
                try:
                    await loop.run_in_executor(None, pub_exec.shutdown, True)
                except Exception:
                    close_failure_count += 1
                    logger.exception(
                        "pubsub publish executor shutdown failed; continuing teardown"
                    )
            if self._publisher is not None:
                pub = self._publisher
                self._publisher = None
                try:
                    await loop.run_in_executor(None, pub.close)
                except Exception:
                    close_failure_count += 1
                    logger.exception(
                        "pubsub publisher.close() failed; continuing teardown"
                    )
            teardown_complete = True
        finally:
            # Defense-in-depth cancel + drain: if an unexpected
            # BaseException escaped the per-step guards above
            # (CancelledError, KeyboardInterrupt, ...) the try body
            # may have bailed before the cancel/gather in the try
            # body ran. Cancel here (cancel() is idempotent, so
            # double-cancel in the happy path is a no-op) and drain
            # so we don't drop pending task references on the next
            # `_pull_tasks.clear()` — asyncio emits "Task was
            # destroyed but it is pending" for orphaned references,
            # and in-flight ack/nack work inside handlers must still
            # get a chance to finish. Awaiting inside a finally during
            # external cancellation is legal on Python 3.9+: the outer
            # BaseException resumes propagating once the finally exits.
            for t in self._pull_tasks:
                t.cancel()
            if self._pull_tasks:
                await asyncio.gather(*self._pull_tasks, return_exceptions=True)
            self._pull_tasks.clear()
            # Drain background close() tasks before clearing strong refs.
            # These coroutines are awaiting non-cancellable
            # ``run_in_executor`` futures that finish the SDK-level
            # close() — cancelling them would be a no-op on the actual
            # close work, but if the event loop tears down right after
            # ``stop()`` returns, any still-running task gets dropped
            # mid-await and the gRPC channel leaks.
            #
            # ``list()`` snapshots the set: each task's done-callback
            # calls ``self._background_tasks.discard(task)`` on
            # completion, which would otherwise mutate the set while
            # ``gather`` is iterating its argument. ``return_exceptions``
            # so a single failing close() can't escape this finally.
            #
            # Ordering note: a concurrent ``_ensure_publisher`` whose
            # shielded await resumes AFTER our finally runs can still
            # spawn a ``_close_post_stop`` task into the now-empty
            # set — that task is drained by the next ``stop()`` (or
            # by the executor thread's own ref chain if no second
            # stop ever happens, since the executor holds the close
            # function across the await).
            if self._background_tasks:
                await asyncio.gather(
                    *list(self._background_tasks), return_exceptions=True
                )
            self._background_tasks.clear()
            self._failed_subscriptions.clear()
            self._stopping = False
            self._warned_missing_start = False
            if teardown_complete:
                # Per-step Exception guards above log-and-continue, so
                # reaching here means every attribute got nulled out
                # (possibly leaking SDK-side threads / gRPC channels
                # from the failing close, which show up in the log).
                # Safe to reset lifecycle flags so a subsequent start()
                # can re-acquire fresh clients.
                self._stopped = False
                self._started = False
                if close_failure_count > 0:
                    # Each failed close leaked its SDK resources (the
                    # attribute was nulled BEFORE the close attempt, so
                    # the object is unrecoverable). A single occurrence
                    # is annoying; repeated stop/start cycles with the
                    # same signature will silently exhaust file
                    # descriptors or thread-pool capacity. Warn loudly
                    # so the accumulation is visible in structured logs.
                    logger.warning(
                        "pubsub stop() had %d close failure(s); "
                        "leaked gRPC resources will accumulate if "
                        "start/stop is called repeatedly with the "
                        "same error signature",
                        close_failure_count,
                    )
            # else: BaseException escaped — leave `_stopped=True` and
            # `_started=True` so `is_healthy` reports False and a
            # concurrent start() bails via its `if self._started:`
            # guard. Without this, start() would overwrite live
            # `_pull_executor` / `_publish_executor` / `_publisher`
            # references whose `shutdown()`/`close()` never ran,
            # permanently orphaning those threads.
