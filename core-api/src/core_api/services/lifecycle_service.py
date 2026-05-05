"""Crystallize + entity-link execution for one tenant.

Pre-CAURA-655 this module also held the periodic scheduler loop and
the SQL-only archive-expired / archive-stale primitives. Both moved
out:

* The scheduler is gone — ``core-operations`` fires the operations on
  cron, fanning out per-org Pub/Sub messages via core-api's
  ``/admin/lifecycle/fanout/<action>`` endpoints (CAURA-655).
* ``archive-expired`` and ``archive-stale`` are now their own per-org
  messages consumed in core-worker via
  ``common.events.lifecycle_handlers``.

What's left here is the LLM-heavy half — crystallize and entity-link —
that still needs core-api's pipeline machinery. CAURA-657 will lift
this onto its own Pub/Sub topics; until then ``run_lifecycle_for_tenant``
remains the single entry point for that work.

Note on import shape: ``get_storage_client`` is imported at the top of
this module so unit tests can patch
``core_api.services.lifecycle_service.get_storage_client`` directly.
``resolve_config`` and ``async_session`` are imported lazily inside the
function body so the same tests can patch them at their *source*
namespaces — the existing test convention this module pre-dates.
"""

from __future__ import annotations

import logging

from core_api.clients.storage_client import get_storage_client

logger = logging.getLogger(__name__)


async def run_lifecycle_for_tenant(
    tenant_id: str,
    fleet_id: str | None = None,
    # Legacy db param kept for call-site compat — unused, the pipeline
    # opens its own session.
    db=None,
) -> dict:
    """Run the LLM-heavy lifecycle steps for one tenant.

    Returns a stats dict with ``crystallization_triggered`` and
    ``entity_linking`` keys; the SQL-only archive counts that used to
    live here moved to the consumer-side audit row's ``stats`` field.
    """
    from core_api.services.organization_settings import resolve_config

    config = await resolve_config(None, tenant_id)

    crystal_triggered = False
    if config.auto_crystallize_enabled:
        from core_api.services.crystallizer_service import run_crystallization

        total = await get_storage_client().count_active(tenant_id, fleet_id)
        if total > 1000:
            await run_crystallization(None, tenant_id, fleet_id, trigger="lifecycle")
            crystal_triggered = True

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
        "crystallization_triggered": crystal_triggered,
        "entity_linking": entity_linking_stats,
    }
