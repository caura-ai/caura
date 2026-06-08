"""Event-bus primitives: event envelope, handler type, ABC for the bus."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Event(BaseModel):
    """Envelope that every message on the bus carries.

    The `payload` is a dict so callers don't need to register pydantic
    schemas upfront; per-topic schema enforcement can be added later as
    separate subclasses if we want stricter typing.
    """

    model_config = ConfigDict(frozen=True)

    event_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    event_type: str
    # Absolute timestamp in UTC — Pub/Sub gives us its own publish time on
    # the subscriber side too, but keeping this in the envelope keeps
    # semantics identical across in-process and Pub/Sub backends.
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    tenant_id: str | None = None
    # Correlation id for tracing a single logical operation across services.
    correlation_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


# A handler is an async callable receiving the full Event envelope. Return
# value is ignored; raising propagates to the bus (in-process buses re-raise
# in tests, Pub/Sub buses nack for redelivery).
EventHandler = Callable[[Event], Awaitable[None]]


class CircularPublishChainError(RuntimeError):
    """Raised by `InProcessEventBus.drain()` when `max_rounds` is hit.

    A dedicated subclass (rather than a string-matched generic
    `RuntimeError`) lets `stop()` distinguish the expected cycle
    scenario from any other programming error without relying on the
    message text staying stable.
    """


class EventBus(ABC):
    """Abstract event bus. Concrete implementations: `InProcessEventBus`,
    `PubSubEventBus`.

    Subscribers are registered at *startup*, not per-call — there's no
    `unsubscribe`. A subscriber registered to a topic gets every event
    published to it until the process exits.
    """

    @abstractmethod
    async def publish(self, topic: str, event: Event) -> None:
        """Publish *event* to *topic*. Fire-and-forget semantics: returns
        once the event has been accepted by the transport, not once it
        has been delivered to every subscriber.
        """

    @abstractmethod
    def subscribe(self, topic: str, handler: EventHandler) -> None:
        """Register *handler* as a subscriber to *topic*. May be called
        at startup only — not thread-safe during `publish`.

        **Delivery guarantee**: handlers must be idempotent. The Pub/Sub
        backend is at-least-once — a message whose ack fails, or whose
        handler raises, gets redelivered. Use `event.event_id` as a
        natural dedup key when the operation isn't inherently
        idempotent.
        """

    async def start(self) -> None:
        """Start any background machinery (subscription listeners, etc.).
        In-process buses treat this as a no-op. Pub/Sub buses spin up
        subscriber pull-tasks here so they can receive messages.
        """

    async def stop(self) -> None:
        """Drain + shut down. Called on graceful shutdown."""

    @property
    def is_healthy(self) -> bool:
        """True when the bus can still deliver events end-to-end.

        Default is True for backends with no external failure modes
        (``InProcessEventBus`` — handlers run in the same process, so
        there's no cross-service state to go wrong). The Pub/Sub backend
        overrides this to flip False when any pull loop has halted on a
        permanent error (subscription missing, SA permission revoked) —
        a service's readiness probe should include this check so a
        misconfigured pod is marked unhealthy instead of silently
        dropping every inbound event while its HTTP surface stays green.

        NOTE: subclasses with external failure modes MUST override this
        property. Inheriting the default-True is only correct for bus
        backends where "delivery" is a synchronous in-process call with
        no transport between producer and consumer that could fail
        asymmetrically.
        """
        return True
