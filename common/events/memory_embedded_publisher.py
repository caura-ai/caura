"""Publish ``Topics.Memory.EMBEDDED`` events.

Convenience wrapper for the worker so the post-PATCH publish doesn't
re-build the ``Event`` envelope + ``MemoryEmbedded`` payload by hand.
The helper validates the payload via Pydantic before publishing —
schema drift surfaces here at publish time rather than on the
consumer side.

Mirrors :func:`common.events.memory_enriched_publisher.publish_memory_enriched`
in shape; consumed by ``core-api``'s ``handle_memory_embedded`` to
fire post-embed contradiction detection.
"""

from __future__ import annotations

from uuid import UUID

from common.events.base import Event
from common.events.factory import get_event_bus
from common.events.memory_embedded import MemoryEmbedded
from common.events.topics import Topics


async def publish_memory_embedded(
    *,
    memory_id: UUID,
    content: str,
    tenant_id: str,
) -> None:
    """Announce successful embedding of *memory_id*.

    Fire-and-forget: ``bus.publish`` returns once the transport has
    accepted the message. Caller (the worker's
    ``handle_embed_request``) wraps in tracking so a transport error
    on the back-channel doesn't nack the upstream PATCH that already
    succeeded — the embedding is durable in storage either way, this
    event is best-effort signalling.
    """
    payload = MemoryEmbedded(
        memory_id=memory_id,
        tenant_id=tenant_id,
        content=content,
    )
    event = Event(
        event_type=Topics.Memory.EMBEDDED,
        tenant_id=tenant_id,
        payload=payload.model_dump(mode="json"),
    )
    bus = get_event_bus()
    await bus.publish(Topics.Memory.EMBEDDED, event)
