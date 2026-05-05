"""Publishers for ``memclaw.lifecycle.<action>-requested`` topics (CAURA-655).

Both publishers share one payload type (:class:`LifecycleArchiveRequest`)
and differ only in the topic — the consumer dispatches on the topic
constant the bus already exposes per-handler.
"""

from __future__ import annotations

from common.events.base import Event
from common.events.factory import get_event_bus
from common.events.lifecycle_archive_request import LifecycleArchiveRequest
from common.events.topics import Topics


async def _publish(
    topic: str,
    *,
    audit_id: int,
    org_id: str,
    triggered_by: str,
    fleet_id: str | None,
) -> None:
    payload = LifecycleArchiveRequest(
        audit_id=audit_id,
        org_id=org_id,
        triggered_by=triggered_by,
        fleet_id=fleet_id,
    )
    event = Event(
        event_type=topic,
        tenant_id=org_id,
        payload=payload.model_dump(mode="json"),
    )
    await get_event_bus().publish(topic, event)


async def publish_archive_expired_request(
    *,
    audit_id: int,
    org_id: str,
    triggered_by: str,
    fleet_id: str | None = None,
) -> None:
    await _publish(
        Topics.Lifecycle.ARCHIVE_EXPIRED_REQUESTED,
        audit_id=audit_id,
        org_id=org_id,
        triggered_by=triggered_by,
        fleet_id=fleet_id,
    )


async def publish_archive_stale_request(
    *,
    audit_id: int,
    org_id: str,
    triggered_by: str,
    fleet_id: str | None = None,
) -> None:
    await _publish(
        Topics.Lifecycle.ARCHIVE_STALE_REQUESTED,
        audit_id=audit_id,
        org_id=org_id,
        triggered_by=triggered_by,
        fleet_id=fleet_id,
    )
