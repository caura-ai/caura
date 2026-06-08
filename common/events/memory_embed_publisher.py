"""Publish ``Topics.Memory.EMBED_REQUESTED`` events.

Convenience wrapper so call sites in the write hot path don't have to
re-build the ``Event`` envelope + ``MemoryEmbedRequest`` payload by hand.
The helper validates the payload via Pydantic before publishing — schema
drift between this caller and ``core_worker.consumer.handle_embed_request``
surfaces here at publish time rather than as a runtime error inside the
consumer (where it would chew through the max-delivery-attempts budget).
"""

from __future__ import annotations

from uuid import UUID

from common.events.base import Event
from common.events.factory import get_event_bus
from common.events.memory_embed_request import MemoryEmbedRequest
from common.events.topics import Topics


async def publish_memory_embed_request(
    *,
    memory_id: UUID,
    content: str,
    tenant_id: str,
    content_hash: str | None = None,
) -> None:
    """Publish one embed-request event for a memory persisted with ``embedding=NULL``.

    Fire-and-forget: ``bus.publish`` returns once the transport has
    accepted the message (Pub/Sub) or the in-process subscriber has been
    invoked (inprocess bus). Callers wrap the call in
    ``track_task(tracked_task(...))`` so a transport error doesn't bubble
    out of the request handler.
    """
    payload = MemoryEmbedRequest(
        memory_id=memory_id,
        tenant_id=tenant_id,
        content=content,
        content_hash=content_hash,
    )
    event = Event(
        event_type=Topics.Memory.EMBED_REQUESTED,
        tenant_id=tenant_id,
        payload=payload.model_dump(mode="json"),
    )
    bus = get_event_bus()
    await bus.publish(Topics.Memory.EMBED_REQUESTED, event)
