"""`get_event_bus()` — single resolver for the active backend.

Reads `EVENT_BUS_BACKEND` (default `inprocess`) plus Pub/Sub-specific env
vars for `pubsub` mode. Returns a singleton so publishers and
subscribers in the same process share the same bus instance.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

from common.events.base import EventBus
from common.events.inprocess import InProcessEventBus
from common.events.pubsub import PubSubEventBus

logger = logging.getLogger(__name__)

_bus: EventBus | None = None
_lock = threading.Lock()


def get_event_bus() -> EventBus:
    """Return the process-wide event bus, constructing on first call.

    Raises ValueError for unknown backend names and RuntimeError when
    Pub/Sub is requested without the required env vars set — upstream
    services should validate these at boot rather than see an obscure
    failure on first publish.
    """
    global _bus
    if _bus is not None:
        return _bus
    with _lock:
        if _bus is not None:
            return _bus
        backend = os.getenv("EVENT_BUS_BACKEND", "inprocess").lower()
        if backend == "inprocess":
            _bus = InProcessEventBus()
        elif backend == "pubsub":
            project_id = os.getenv("GCP_PROJECT_ID") or os.getenv(
                "EVENT_BUS_PROJECT_ID"
            )
            sub_prefix = os.getenv("EVENT_BUS_SUBSCRIPTION_PREFIX")
            if not project_id or not sub_prefix:
                raise RuntimeError(
                    "EVENT_BUS_BACKEND=pubsub requires GCP_PROJECT_ID (or "
                    "EVENT_BUS_PROJECT_ID) and EVENT_BUS_SUBSCRIPTION_PREFIX"
                )
            # Environment identity for the cross-env fan-out guard. Two
            # environments sharing one GCP project also share its topic
            # namespace, so Pub/Sub delivers every published message to
            # *both* envs' subscriptions; the bus uses this to stamp
            # outgoing messages and drop foreign copies (see
            # ``PubSubEventBus`` docstring). Dedicated ``EVENT_BUS_ENV``
            # takes precedence so the bus identity can be decoupled from
            # the broader ``ENVIRONMENT`` if needed; falling back to
            # ``ENVIRONMENT`` means existing deployments (already set to a
            # per-environment value) get the guard with no infra change.
            # Strip before the fallback so a whitespace-only EVENT_BUS_ENV
            # (truthy) doesn't short-circuit the ``or`` and block the
            # ENVIRONMENT fallback; empty collapses to None (guard disabled).
            event_bus_env = os.getenv("EVENT_BUS_ENV", "").strip()
            env = event_bus_env or os.getenv("ENVIRONMENT", "").strip() or None
            if not env:
                # Fail-soft: the guard stays disabled (every message is
                # treated as same-env, i.e. today's behaviour). Warn so a
                # misconfigured multi-env deploy is visible rather than
                # silently leaking across environments.
                logger.warning(
                    "EVENT_BUS_BACKEND=pubsub but neither EVENT_BUS_ENV nor "
                    "ENVIRONMENT is set — the cross-environment fan-out guard "
                    "is DISABLED; messages from sibling environments sharing "
                    "this project's topics will not be dropped."
                )
            kwargs: dict[str, Any] = {"env": env}
            raw_max = os.getenv("EVENT_BUS_PUBSUB_MAX_MESSAGES")
            if raw_max:
                try:
                    max_messages = int(raw_max)
                except ValueError as exc:
                    raise RuntimeError(
                        f"EVENT_BUS_PUBSUB_MAX_MESSAGES must be a positive int <= 1000, got {raw_max!r}"
                    ) from exc
                # Pub/Sub's ``pull()`` API rejects ``max_messages > 1000`` with
                # INVALID_ARGUMENT at first pull. Fail fast at boot instead so
                # a typo in the env var doesn't surface as a runtime error
                # mid-traffic.
                if not 1 <= max_messages <= 1000:
                    raise RuntimeError(
                        f"EVENT_BUS_PUBSUB_MAX_MESSAGES must be a positive int <= 1000, got {raw_max!r}"
                    )
                kwargs["max_messages"] = max_messages
            # Fail fast at factory resolution (service boot) rather than on
            # first publish, so a misconfigured deploy that's missing the
            # Pub/Sub SDK surfaces immediately instead of after the first
            # real event.
            PubSubEventBus._ensure_pubsub_sdk()
            _bus = PubSubEventBus(project_id, sub_prefix, **kwargs)
        else:
            raise ValueError(
                f"Unknown EVENT_BUS_BACKEND {backend!r}; expected inprocess or pubsub"
            )
        return _bus


async def reset_event_bus_for_testing() -> None:
    """Clear the cached bus so tests can exercise different backends or
    isolate state between cases. Production code must NOT call this.

    Async so we can ``await current.stop()`` before discarding — without
    this, a ``PubSubEventBus`` would leak its ``ThreadPoolExecutor`` +
    gRPC ``SubscriberClient`` each test, and ``InProcessEventBus`` would
    leave pending asyncio tasks behind. Callers must use ``await`` (test
    fixtures that used the sync form need updating).
    """
    global _bus
    with _lock:
        current = _bus
        _bus = None
    if current is not None:
        await current.stop()
