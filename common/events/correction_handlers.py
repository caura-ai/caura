"""Consumer for ``<brand>.memory.corrected`` (correction propagation, CP-10).

Turns each correction event into a ``memory_correction`` record and the list
of agents to tell: every agent that read the memory before it was corrected
(``memory_exposure``), less the agent that corrected it, at most 50. The record
starts as a ``preview``; nothing is sent from here (CP-11 sends).

Lives in ``common/`` so the same code runs in core-worker (the SaaS
subscriber) and in core-api on the in-process bus (OSS standalone, where no
worker process exists). The storage round-trip goes through a small adapter
the host service supplies; this module never imports a service's storage
code. No LLM calls.

Failure modes, as the suppression consumer's:

* a payload that does not parse is dropped (no retry makes it parse);
* a storage failure raises, so Pub/Sub redelivers (DLQ after the configured
  attempts). Storage is idempotent on the envelope's ``event_id``, so a
  redelivery after a lost response writes nothing twice.
"""

from __future__ import annotations

import logging

from pydantic import ValidationError

from common.events.base import Event
from common.events.factory import get_event_bus
from common.events.memory_corrected import MemoryCorrected
from common.events.topics import Topics

logger = logging.getLogger(__name__)


class CorrectionStorageAdapter:
    """Protocol-shaped adapter the host service implements.

    A bare base rather than ``typing.Protocol`` for the reason
    :class:`common.events.suppression_handlers.SuppressionStorageAdapter`
    gives: no circular import, and a test fake overrides the one method.
    """

    async def record_correction(self, *, tenant_id: str, body: dict) -> dict:
        """POST one correction to core-storage-api (``/memory-corrections``).

        ``body`` is JSON-safe: ``event_id``, ``memory_id``, ``kind``,
        ``actor_kind``, ``actor_id``, ``corrected_at``, ``old_summary``,
        ``new_summary``, ``detail``. Returns storage's response.
        """
        raise NotImplementedError


def correction_body(event: Event, payload: MemoryCorrected) -> dict:
    """The storage body for one correction event."""
    return {
        "event_id": str(event.event_id),
        "memory_id": str(payload.memory_id),
        "kind": payload.kind,
        "actor_kind": payload.actor.kind,
        "actor_id": payload.actor.id,
        "corrected_at": payload.corrected_at.isoformat(),
        "old_summary": payload.old_summary,
        "new_summary": payload.new_summary,
        "detail": payload.detail,
    }


async def _handle_memory_corrected(
    event: Event, *, adapter: CorrectionStorageAdapter
) -> None:
    """Subscriber for ``<brand>.memory.corrected``: record the correction and its recipients."""
    try:
        payload = MemoryCorrected.model_validate(event.payload)
    except ValidationError:
        logger.exception(
            "dropping malformed memory-corrected payload",
            extra={
                "event_type": event.event_type,
                "event_id": str(event.event_id),
                "dropped": True,
            },
        )
        return

    try:
        result = await adapter.record_correction(
            tenant_id=payload.tenant_id, body=correction_body(event, payload)
        )
    except Exception:
        logger.exception(
            "memory-corrected record failed; will redeliver",
            extra={
                "event_id": str(event.event_id),
                "tenant_id": payload.tenant_id,
                "memory_id": str(payload.memory_id),
            },
        )
        raise

    logger.info(
        "memory correction recorded",
        extra={
            "event_id": str(event.event_id),
            "tenant_id": payload.tenant_id,
            "memory_id": str(payload.memory_id),
            "kind": payload.kind,
            "correction_id": result.get("id"),
            "first_delivery": result.get("created"),
            "recipients": result.get("recipients"),
            "truncated": result.get("truncated"),
        },
    )


def register_correction_consumer(adapter: CorrectionStorageAdapter) -> None:
    """Wire :func:`_handle_memory_corrected` against ``Topics.Memory.CORRECTED``.

    Called once at startup, before ``bus.start()``: by core-worker on SaaS, and
    by core-api only on the in-process bus.
    """
    bus = get_event_bus()

    async def _bound(event: Event) -> None:
        await _handle_memory_corrected(event, adapter=adapter)

    bus.subscribe(Topics.Memory.CORRECTED, _bound)
