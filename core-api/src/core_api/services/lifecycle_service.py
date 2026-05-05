"""Lifecycle automation — periodic archival of expired and stale memories."""

import asyncio
import logging

from core_api.clients.storage_client import get_storage_client
from core_api.constants import (
    LIFECYCLE_INTERVAL_HOURS,
    LIFECYCLE_STALE_ARCHIVE_WEIGHT,
)

logger = logging.getLogger(__name__)


async def lifecycle_scheduler() -> None:
    """Background scheduler: runs lifecycle automation on an interval."""
    logger.info("Lifecycle scheduler started (interval=%dh)", LIFECYCLE_INTERVAL_HOURS)
    while True:
        await asyncio.sleep(LIFECYCLE_INTERVAL_HOURS * 3600)
        try:
            await _run_lifecycle_cycle()
        except Exception:
            logger.exception("Lifecycle cycle failed")


async def _run_lifecycle_cycle() -> None:
    """Run lifecycle automation for all tenants."""
    from core_api.services.organization_settings import resolve_config
    from core_api.standalone import get_standalone_tenant_id

    tenant_ids = [get_standalone_tenant_id()]

    for tid in tenant_ids:
        try:
            config = await resolve_config(None, tid)
            if not config.lifecycle_automation_enabled:
                continue
            stats = await run_lifecycle_for_tenant(tid)
            if stats["expired_archived"] or stats["stale_archived"] or stats.get("entity_linking"):
                logger.info("Lifecycle for tenant=%s: %s", tid, stats)
        except Exception:
            logger.exception("Lifecycle failed for tenant %s", tid)


async def run_lifecycle_for_tenant(
    tenant_id: str,
    fleet_id: str | None = None,
    # Legacy db param kept for call-site compat — unused, storage client handles DB.
    db=None,
) -> dict:
    """Execute lifecycle transitions for a tenant. Returns stats."""
    expired_count = await _archive_expired_memories(tenant_id, fleet_id)
    stale_count = await _archive_stale_memories(tenant_id, fleet_id)

    crystal_triggered = False
    from core_api.services.organization_settings import resolve_config

    config = await resolve_config(None, tenant_id)

    if config.auto_crystallize_enabled:
        total = await _count_active_memories(tenant_id, fleet_id)
        if total > 1000:
            from core_api.services.crystallizer_service import run_crystallization

            await run_crystallization(None, tenant_id, fleet_id, trigger="lifecycle")
            crystal_triggered = True

    # Entity linking — discover cross-links for under-connected memories
    entity_linking_stats: dict = {}
    if config.auto_entity_linking_enabled:
        try:
            from core_api.db.session import async_session
            from core_api.pipeline.compositions.entity_linking import (
                build_full_entity_linking_pipeline,
            )
            from core_api.pipeline.context import PipelineContext

            async with async_session() as db:
                ctx = PipelineContext(
                    db=db,
                    data={
                        "tenant_id": tenant_id,
                        **({"fleet_id": fleet_id} if fleet_id else {}),
                    },
                )
                pipeline = build_full_entity_linking_pipeline()
                result = await pipeline.run(ctx)
                await db.commit()
                entity_linking_stats = {
                    "links_created": ctx.data.get("links_created", 0),
                    "steps": result.step_count,
                }
        except Exception:
            logger.warning(
                "Entity linking failed for tenant %s (non-fatal)",
                tenant_id,
                exc_info=True,
            )

    return {
        "expired_archived": expired_count,
        "stale_archived": stale_count,
        "crystallization_triggered": crystal_triggered,
        "entity_linking": entity_linking_stats,
    }


async def _archive_expired_memories(tenant_id: str, fleet_id: str | None = None) -> int:
    """Transition expired-but-active memories to 'outdated'."""
    sc = get_storage_client()
    return await sc.archive_expired(tenant_id, fleet_id)


async def _archive_stale_memories(tenant_id: str, fleet_id: str | None = None) -> int:
    """Archive old, never-recalled, low-weight memories."""
    sc = get_storage_client()
    return await sc.archive_stale(tenant_id, fleet_id, max_weight=LIFECYCLE_STALE_ARCHIVE_WEIGHT)


async def _count_active_memories(tenant_id: str, fleet_id: str | None = None) -> int:
    """Count active non-deleted memories for a tenant."""
    sc = get_storage_client()
    return await sc.count_active(tenant_id, fleet_id)
