"""Publishers for ``memclaw.lifecycle.<action>-requested`` topics
(CAURA-655 archive, CAURA-656 purge, CAURA-657 pipeline).

Most publishers share :class:`LifecycleArchiveRequest` (no per-message
data); ``purge`` uses :class:`LifecyclePurgeRequest` because it carries
``retention_days``. The consumer dispatches on the topic the bus
exposes per-handler.
"""

from __future__ import annotations

from common.events.base import Event
from common.events.factory import get_event_bus
from common.events.lifecycle_archive_request import (
    LifecycleArchiveRequest,
    LifecycleRequestBase,
)
from common.events.lifecycle_purge_request import LifecyclePurgeRequest
from common.events.topics import Topics


async def _publish(topic: str, payload: LifecycleRequestBase) -> None:
    event = Event(
        event_type=topic,
        tenant_id=payload.org_id,
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
        LifecycleArchiveRequest(
            audit_id=audit_id,
            org_id=org_id,
            triggered_by=triggered_by,
            fleet_id=fleet_id,
        ),
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
        LifecycleArchiveRequest(
            audit_id=audit_id,
            org_id=org_id,
            triggered_by=triggered_by,
            fleet_id=fleet_id,
        ),
    )


async def publish_purge_soft_deleted_request(
    *,
    audit_id: int,
    org_id: str,
    triggered_by: str,
    retention_days: int,
    fleet_id: str | None = None,
) -> None:
    await _publish(
        Topics.Lifecycle.PURGE_SOFT_DELETED_REQUESTED,
        LifecyclePurgeRequest(
            audit_id=audit_id,
            org_id=org_id,
            triggered_by=triggered_by,
            fleet_id=fleet_id,
            retention_days=retention_days,
        ),
    )


async def publish_crystallize_request(
    *,
    audit_id: int,
    org_id: str,
    triggered_by: str,
    fleet_id: str | None = None,
) -> None:
    """CAURA-657: trigger crystallization for one org. Reuses the
    archive payload — the action carries no per-message data beyond
    the shared base. ``auto_crystallize_enabled`` is consulted by the
    consumer (which lives in core-api), not the publisher; fanout
    fires blanket and the consumer no-ops orgs that have it disabled.
    """
    await _publish(
        Topics.Lifecycle.CRYSTALLIZE_REQUESTED,
        LifecycleArchiveRequest(
            audit_id=audit_id,
            org_id=org_id,
            triggered_by=triggered_by,
            fleet_id=fleet_id,
        ),
    )


async def publish_entity_link_request(
    *,
    audit_id: int,
    org_id: str,
    triggered_by: str,
    fleet_id: str | None = None,
) -> None:
    """CAURA-657: trigger entity-link cross-link discovery for one org.
    Same payload shape as archive ops. ``auto_entity_linking_enabled``
    is consulted by the consumer.
    """
    await _publish(
        Topics.Lifecycle.ENTITY_LINK_REQUESTED,
        LifecycleArchiveRequest(
            audit_id=audit_id,
            org_id=org_id,
            triggered_by=triggered_by,
            fleet_id=fleet_id,
        ),
    )
