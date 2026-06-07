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
from common.events.lifecycle_forge_request import LifecycleForgeDistillRequest
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


async def publish_insights_request(
    *,
    audit_id: int,
    org_id: str,
    triggered_by: str,
    fleet_id: str | None = None,
) -> None:
    """Trigger insights discovery (focus='discover') for one org.
    Same payload shape as the other pipeline ops. ``auto_insights_enabled``
    is consulted by the consumer (opt-in, default off); fanout fires
    blanket and the consumer no-ops orgs that have it disabled or whose
    corpus hasn't grown since the last insights run.
    """
    await _publish(
        Topics.Lifecycle.INSIGHTS_REQUESTED,
        LifecycleArchiveRequest(
            audit_id=audit_id,
            org_id=org_id,
            triggered_by=triggered_by,
            fleet_id=fleet_id,
        ),
    )


async def publish_forge_distill_request(
    *,
    audit_id: int,
    org_id: str,
    triggered_by: str,
    run_label: str,
    fleet_id: str | None = None,
    freshness_window_days: int | None = None,
    min_cluster_size: int | None = None,
    min_distinct_agents: int | None = None,
    llm_tokens_per_run: int | None = None,
    max_writes_per_run: int | None = None,
    dry_run: bool = False,
) -> None:
    """Skill Factory SF-007: trigger one Forge distillation run for an
    org/fleet. Per-run override knobs default to ``None`` so the
    consumer falls through to
    ``org_settings.skills_factory.forge.*``. Phase 0 ships only the
    publisher + a no-op handler; the real worker (cluster fingerprint,
    LLM distill, gating, scan) arrives in Phase 1. See
    :class:`~common.events.lifecycle_forge_request.LifecycleForgeDistillRequest`.
    """
    await _publish(
        Topics.Lifecycle.FORGE_DISTILL_REQUESTED,
        LifecycleForgeDistillRequest(
            audit_id=audit_id,
            org_id=org_id,
            triggered_by=triggered_by,
            fleet_id=fleet_id,
            run_label=run_label,
            freshness_window_days=freshness_window_days,
            min_cluster_size=min_cluster_size,
            min_distinct_agents=min_distinct_agents,
            llm_tokens_per_run=llm_tokens_per_run,
            max_writes_per_run=max_writes_per_run,
            dry_run=dry_run,
        ),
    )
