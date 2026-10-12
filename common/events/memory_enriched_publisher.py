"""Publish ``Topics.Memory.ENRICHED`` events (CAURA-595).

Convenience wrapper for the worker so the post-PATCH publish doesn't
re-build the ``Event`` envelope + ``MemoryEnriched`` payload by hand.
The helper validates the payload via Pydantic before publishing —
schema drift surfaces here at publish time rather than on the
consumer side.

Mirrors :func:`common.events.memory_embed_publisher.publish_memory_embed_request`
in shape; consumed by ``core-api`` once it registers a back-channel
subscriber (also CAURA-595).
"""

from __future__ import annotations

from uuid import UUID

from common.events.base import Event
from common.events.factory import get_event_bus
from common.events.memory_enriched import MemoryEnriched
from common.events.topics import Topics


async def publish_memory_enriched(
    *,
    memory_id: UUID,
    content: str,
    tenant_id: str,
    retrieval_hint: str = "",
) -> None:
    """Announce successful enrichment of *memory_id*.

    Fire-and-forget: ``bus.publish`` returns once the transport has
    accepted the message. Caller (the worker's
    ``handle_enrich_request``) wraps in tracking so a transport error
    on the back-channel doesn't nack the upstream PATCH that already
    succeeded — the enrichment is durable in storage either way, this
    event is best-effort signalling.
    """
    payload = MemoryEnriched(
        memory_id=memory_id,
        tenant_id=tenant_id,
        content=content,
        retrieval_hint=retrieval_hint,
    )
    event = Event(
        event_type=Topics.Memory.ENRICHED,
        tenant_id=tenant_id,
        payload=payload.model_dump(mode="json"),
    )
    bus = get_event_bus()
    await bus.publish(Topics.Memory.ENRICHED, event)
