"""Event-bus library for cross-service messaging.

Two implementations behind one ABC:

- `InProcessEventBus`: handler registry dispatched via asyncio tasks.
  Default for standalone / tests / dev. Zero external dependencies.
- `PubSubEventBus`: GCP Pub/Sub. Used in SaaS deployments where
  publisher and subscribers run in different services.

Callers resolve the concrete bus via `get_event_bus()`, which reads
`EVENT_BUS_BACKEND` (`inprocess` | `pubsub`; default `inprocess`).
"""

from common.events.base import (
    CircularPublishChainError,
    Event,
    EventBus,
    EventHandler,
)
from common.events.factory import get_event_bus
from common.events.inprocess import InProcessEventBus
from common.events.lifecycle_publishers import (
    publish_archive_expired_request,
    publish_archive_stale_request,
)
from common.events.memory_embed_publisher import publish_memory_embed_request
from common.events.memory_enrich_publisher import publish_memory_enrich_request
from common.events.memory_enriched_publisher import publish_memory_enriched
from common.events.pubsub import PubSubEventBus
from common.events.topics import Topics

# Intentionally NOT re-exported:
# - `reset_event_bus_for_testing` — test-only utility. Import directly from
#   `common.events.factory` so it stays off the production API surface.

__all__ = [
    "CircularPublishChainError",
    "Event",
    "EventBus",
    "EventHandler",
    "InProcessEventBus",
    "PubSubEventBus",
    "Topics",
    "get_event_bus",
    "publish_archive_expired_request",
    "publish_archive_stale_request",
    "publish_memory_embed_request",
    "publish_memory_enrich_request",
    "publish_memory_enriched",
]
