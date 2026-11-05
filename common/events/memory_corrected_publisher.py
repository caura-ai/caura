"""Publish ``Topics.Memory.CORRECTED`` events.

Builds the ``Event`` envelope around a validated
:class:`~common.events.memory_corrected.MemoryCorrected` payload, so schema
drift surfaces at publish time rather than in a consumer.
"""

from __future__ import annotations

from common.events.base import Event
from common.events.factory import get_event_bus
from common.events.memory_corrected import MemoryCorrected
from common.events.topics import Topics


async def publish_memory_corrected(payload: MemoryCorrected) -> None:
    """Announce a correction. Returns once the transport accepted the message.

    The caller (core-api's ``memory_corrections``) runs this off the request
    path and swallows any error: the correction is already committed, and the
    event is best-effort signalling.
    """
    event = Event(
        event_type=Topics.Memory.CORRECTED,
        tenant_id=payload.tenant_id,
        payload=payload.model_dump(mode="json"),
    )
    await get_event_bus().publish(Topics.Memory.CORRECTED, event)
