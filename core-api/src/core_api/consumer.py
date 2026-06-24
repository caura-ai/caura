"""Pub/Sub consumer for core-api.

Subscribes core-api to two back-channels published by core-worker
after the async write path lands a memory row:

* ``Topics.Memory.ENRICHED`` (CAURA-595) — fired after a successful
  enrichment PATCH.
* ``Topics.Memory.EMBEDDED`` (loadtest-7 fix) — fired after a
  successful embedding PATCH.

Both consumers run :func:`detect_contradictions_async`. Two events
trigger the detector because either the embed or the enrich worker
can land first, and the detector needs both fields available; whichever
event arrives second sees the other side present and runs detection.
The detector is idempotent under repeated calls on the same row, so
the rare case where both events trigger detection back-to-back doesn't
double-write contradiction rows.

Atomic-fact fan-out (parent → child memories) is intentionally not
handled here. The worker drops ``EnrichmentResult.atomic_facts``
entirely (see ``_ENRICHMENT_UNROUTED_FIELDS`` in
``core-worker/src/core_worker/consumer.py``), so they never reach
storage. Persisting them at the worker side and fanning out from a
storage fetch is a separate piece of work.
"""

from __future__ import annotations

import logging

from pydantic import ValidationError

from common.events.base import Event
from common.events.factory import get_event_bus
from common.events.memory_embedded import MemoryEmbedded
from common.events.memory_enriched import MemoryEnriched
from common.events.org_settings_changed_event import OrgSettingsChangedEvent
from common.events.topics import Topics
from core_api.clients.storage_client import get_storage_client
from core_api.services.contradiction_detector import detect_contradictions_async
from core_api.services.governance_remediation import remediate_after_enrichment
from core_api.services.organization_settings import invalidate_cache, resolve_config

logger = logging.getLogger(__name__)


async def handle_memory_enriched(event: Event) -> None:
    """Process one ``Topics.Memory.ENRICHED`` event.

    Loads the memory row from storage (so we have the embedding +
    fleet_id that the minimal payload doesn't carry) and dispatches to
    ``detect_contradictions_async`` when the embedding has already
    landed. Schema-drift / malformed payloads ack-drop with a loud
    ``dropped=True`` log entry so a poison message can't loop the
    subscription.

    The detection coroutine is awaited inline rather than spawned via
    ``track_task``: it owns its own ``try/except`` and never raises, so
    the bus always acks regardless of detection outcome — but the await
    delays the ack until detection completes, which prevents
    redelivery from stacking concurrent detections on the same memory.
    """
    try:
        payload = MemoryEnriched(**event.payload)
    except (ValidationError, TypeError):
        # ``ValidationError`` covers shape drift (missing required
        # fields, wrong types). ``TypeError`` covers ``event.payload``
        # not being a mapping at all — ``**non_dict`` raises before
        # Pydantic ever sees it. Both are poison-message conditions
        # that re-raising would just nack-loop on; ack-drop loudly.
        logger.exception(
            "dropping malformed memory-enriched payload",
            extra={
                "event_type": event.event_type,
                "event_id": str(event.event_id),
                "dropped": True,
            },
        )
        return

    sc = get_storage_client()
    memory = await sc.get_memory(str(payload.memory_id))
    if memory is None:
        # Row was deleted between the worker's PATCH and our handler
        # picking up the event. Common-enough race to ack-drop without
        # noise; matches the worker's 404 handling.
        logger.info(
            "memory-enriched: target row missing; ack-dropping",
            extra={
                "memory_id": str(payload.memory_id),
                "tenant_id": payload.tenant_id,
            },
        )
        return

    # Fast-mode governance remediation: the worker just PATCHed the LLM's
    # contains_pii / business_relevance onto the row; apply the tenant's
    # configured action (drop / keep-private / flag) now — the fast-mode
    # counterpart to the synchronous GovernanceDecision step. ``resolve_config``
    # tolerates a None session (cache-first; cold-miss opens its own).
    gov_cfg = await resolve_config(payload.tenant_id)
    if await remediate_after_enrichment(memory, gov_cfg):
        # Row was dropped by policy — skip contradiction detection on it.
        logger.info(
            "memory-enriched: row dropped by governance policy; ack",
            extra={"memory_id": str(payload.memory_id), "tenant_id": payload.tenant_id},
        )
        return

    embedding = memory.get("embedding")
    if not embedding:
        # Embed worker hasn't completed yet (or its PATCH 404'd).
        # ``deferred_reason`` is structured so a Cloud Logging metric
        # can scrape the count of skipped detections — without it,
        # there's no production visibility into how often the race
        # fires.
        logger.info(
            "memory-enriched: embedding not yet present; deferring contradiction detection",
            extra={
                "memory_id": str(payload.memory_id),
                "tenant_id": payload.tenant_id,
                "deferred_reason": "embedding_missing",
            },
        )
        return

    fleet_id = memory.get("fleet_id")

    # Pass the already-fetched row through so the detector skips a
    # redundant ``sc.get_memory`` (it still re-checks ``deleted_at``
    # for the soft-delete-during-detection race).
    await detect_contradictions_async(
        payload.memory_id,
        payload.tenant_id,
        fleet_id,
        payload.content,
        embedding,
        new_memory=memory,
    )

    logger.info(
        "memory-enriched processed",
        extra={
            "memory_id": str(payload.memory_id),
            "tenant_id": payload.tenant_id,
            "fleet_id": fleet_id,
            "embedding_dim": len(embedding),
        },
    )


async def handle_memory_embedded(event: Event) -> None:
    """Process one ``Topics.Memory.EMBEDDED`` event.

    Mirror of :func:`handle_memory_enriched` for the embed-finishes-
    after-enrich ordering. Loads the memory row from storage (so we
    have the embedding the minimal payload doesn't carry, plus
    ``fleet_id``) and dispatches to :func:`detect_contradictions_async`
    once the embedding has landed. Schema-drift / malformed payloads
    ack-drop with a loud ``dropped=True`` log entry so a poison
    message can't loop the subscription.

    Like the enriched handler, the detection coroutine is awaited
    inline rather than spawned via ``track_task``: it owns its own
    ``try/except`` and never raises, so the bus always acks regardless
    of detection outcome — but the await delays the ack until detection
    completes, which prevents redelivery from stacking concurrent
    detections on the same memory.
    """
    try:
        payload = MemoryEmbedded(**event.payload)
    except (ValidationError, TypeError):
        logger.exception(
            "dropping malformed memory-embedded payload",
            extra={
                "event_type": event.event_type,
                "event_id": str(event.event_id),
                "dropped": True,
            },
        )
        return

    sc = get_storage_client()
    memory = await sc.get_memory(str(payload.memory_id))
    if memory is None:
        # Row was deleted between the worker's PATCH and our handler
        # picking up the event. Common-enough race to ack-drop without
        # noise; matches the worker's 404 handling.
        logger.info(
            "memory-embedded: target row missing; ack-dropping",
            extra={
                "memory_id": str(payload.memory_id),
                "tenant_id": payload.tenant_id,
            },
        )
        return

    embedding = memory.get("embedding")
    if not embedding:
        # Should not happen — the embed worker just PATCHed it before
        # publishing — but guard against a soft-delete or column
        # rewrite race rather than passing None into the detector.
        logger.warning(
            "memory-embedded: embedding missing on read-back; ack-dropping",
            extra={
                "memory_id": str(payload.memory_id),
                "tenant_id": payload.tenant_id,
            },
        )
        return

    fleet_id = memory.get("fleet_id")

    await detect_contradictions_async(
        payload.memory_id,
        payload.tenant_id,
        fleet_id,
        payload.content,
        embedding,
        new_memory=memory,
    )

    logger.info(
        "memory-embedded processed",
        extra={
            "memory_id": str(payload.memory_id),
            "tenant_id": payload.tenant_id,
            "fleet_id": fleet_id,
            "embedding_dim": len(embedding),
        },
    )


async def handle_org_settings_changed(event: Event) -> None:
    """Process one ``Topics.Org.SETTINGS_CHANGED`` event (CAURA-571).

    Drops the affected org's entry from THIS process's settings cache, so a
    settings change made on any worker/instance takes effect here promptly
    instead of waiting out the per-process 5-min TTL. The publishing worker
    already invalidated its own cache locally; this is the fan-out to every
    other process (the topic is subscribed with ``broadcast=True``).

    Idempotent — evicting an absent or already-fresh entry is a harmless no-op —
    so the at-least-once bus may redeliver freely. A malformed payload is
    ack-dropped (no retry will make it parse).
    """
    try:
        payload = OrgSettingsChangedEvent.model_validate(event.payload)
    except ValidationError:
        logger.exception(
            "dropping malformed org-settings-changed payload",
            extra={
                "event_type": event.event_type,
                "event_id": str(event.event_id),
                "dropped": True,
            },
        )
        return
    invalidate_cache(payload.org_id)
    logger.info(
        "settings cache invalidated via broadcast",
        extra={"event_id": str(event.event_id), "org_id": payload.org_id},
    )


def register_consumers() -> None:
    """Wire the consumers into the event bus.

    Must run before ``bus.start()`` — the Pub/Sub backend spawns its
    pull loops in ``start()`` from the current handler registry, so a
    late ``subscribe()`` would silently orphan the handler.
    """
    bus = get_event_bus()
    bus.subscribe(Topics.Memory.ENRICHED, handle_memory_enriched)
    bus.subscribe(Topics.Memory.EMBEDDED, handle_memory_embedded)
    # Broadcast: EVERY core-api process must drop its settings cache, not just
    # one — so this uses a per-process subscription, unlike the work-queue
    # consumers above (CAURA-571).
    bus.subscribe(Topics.Org.SETTINGS_CHANGED, handle_org_settings_changed, broadcast=True)
