"""Consumer for ``memclaw.org.suppression-changed`` (CAURA-694).

Lives in ``common/`` so the same code can run in core-worker (the
default SaaS subscriber) or in core-api (if a future OSS-standalone
deployment grows a subscriber rather than ignoring the topic).

The handler delegates the storage round-trip to a small adapter the
host service supplies; this module never imports core-api / core-worker
storage code. Each event covers a list of tenant_ids — we iterate and
call the adapter once per tenant_id. Per-tenant failures are logged
but the message is acked only if every tenant succeeded; a single
failure re-raises so Pub/Sub redelivers (DLQ on max-delivery exhaustion).

Re-raising on partial failure (vs. ack-with-warning) is the safer
trade-off here: missing a suppression write opens an authz hole —
better to retry until the row lands or DLQs to on-call.
"""

from __future__ import annotations

import logging

from pydantic import ValidationError

from common.events.base import Event
from common.events.factory import get_event_bus
from common.events.org_suppression_event import OrgSuppressionEvent
from common.events.topics import Topics

logger = logging.getLogger(__name__)


class SuppressionStorageAdapter:
    """Protocol-shaped adapter the host service implements.

    A bare base (rather than ``typing.Protocol``) so the import doesn't
    pull a circular dep into common, and so a test fake can subclass
    and override just the one method. The handler treats this as a
    duck-typed callable surface — the runtime check is the
    ``set_tenant_suppression`` lookup itself.
    """

    async def set_tenant_suppression(
        self, *, tenant_id: str, action: str, updated_by: str | None
    ) -> None:
        raise NotImplementedError


async def _handle_suppression_changed(
    event: Event, *, adapter: SuppressionStorageAdapter
) -> None:
    """Subscriber for ``memclaw.org.suppression-changed``.

    Iterates the tenant_ids in the payload and upserts each into the
    suppression mirror via the adapter. Any per-tenant failure raises
    so Pub/Sub redelivers (and DLQs after the configured max-delivery
    attempts in CAURA-695). A malformed payload is dropped (no
    redelivery) because no number of retries will make it parse.
    """
    try:
        # ``model_validate`` is the idiomatic Pydantic v2 entry point and
        # is marginally more defensive than ``OrgSuppressionEvent(**…)``
        # — it raises ``ValidationError`` for both bad shapes and
        # unexpected input types (the kwargs splat raises ``TypeError``
        # on a non-dict, which would skip this except block). Bot
        # review round 2 on PR #244 (suggestion).
        payload = OrgSuppressionEvent.model_validate(event.payload)
    except ValidationError:
        logger.exception(
            "dropping malformed org-suppression payload",
            extra={
                "event_type": event.event_type,
                "event_id": str(event.event_id),
                "dropped": True,
            },
        )
        return

    # Propagate the correlation_id from the envelope to the audit
    # ``updated_by`` field — without a separate identity carrier on the
    # event, the correlation id is the best causal link back to the
    # platform-admin-api request that triggered the publish.
    updated_by = event.correlation_id or "core-worker"

    for tenant_id in payload.tenant_ids:
        try:
            await adapter.set_tenant_suppression(
                tenant_id=tenant_id,
                action=payload.action,
                updated_by=updated_by,
            )
        except Exception:
            logger.exception(
                "suppression upsert failed; will redeliver",
                extra={
                    "event_id": str(event.event_id),
                    "tenant_id": tenant_id,
                    "action": payload.action,
                },
            )
            # Re-raise so the bus nacks the message → redelivered (subject
            # to max-delivery-attempts → DLQ). Acking on partial failure
            # would leave the suppression mirror inconsistent with the
            # enterprise authoritative state.
            raise

    logger.info(
        "org suppression mirrored",
        extra={
            "event_id": str(event.event_id),
            "action": payload.action,
            "tenants_updated": len(payload.tenant_ids),
        },
    )


def register_suppression_consumer(adapter: SuppressionStorageAdapter) -> None:
    """Wire :func:`_handle_suppression_changed` against
    ``Topics.Org.SUPPRESSION_CHANGED`` on the active event bus.

    Called once at startup by core-worker (SaaS). OSS standalone has no
    publisher today; calling this in pure OSS is harmless — the
    subscription just sits idle.
    """
    bus = get_event_bus()

    async def _bound(event: Event) -> None:
        await _handle_suppression_changed(event, adapter=adapter)

    bus.subscribe(Topics.Org.SUPPRESSION_CHANGED, _bound)
