"""Helpers around the lifecycle_audit storage routes + per-action
publisher kwargs (CAURA-655 / CAURA-656).

Three helpers:

* :func:`audit_begin` — creates a ``pending`` row and returns its id.
  Called by the fanout endpoint just before each per-org Pub/Sub
  publish, so the published event carries the row id.
* :func:`make_storage_adapter` — wraps the storage client into the
  :class:`LifecycleStorageAdapter` shape the shared handler expects.
  Used in OSS standalone where core-api itself subscribes to the
  in-process bus (no separate worker process).
* :func:`resolve_publisher_kwargs` — per-action settings → publisher
  kwarg map (e.g. CAURA-656 purge needs ``retention_days`` from each
  org's ``lifecycle.memory_retention_days`` setting).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from core_api.clients.storage_client import CoreStorageClient
from core_api.constants import LIFECYCLE_STALE_ARCHIVE_WEIGHT
from core_api.services.organization_settings import resolve_config

# Org needs at least this many active memories before lifecycle
# crystallization runs — below that the corpus is too small for the
# auto-curate step to produce useful clusters, and the report row +
# hygiene checks would be wasted compute. Matches the threshold in
# the deleted core_api.services.lifecycle_service.
_CRYSTALLIZE_MIN_ACTIVE_MEMORIES = 1000

logger = logging.getLogger(__name__)


async def audit_begin(
    storage: CoreStorageClient,
    *,
    action: str,
    org_id: str,
    triggered_by: str,
) -> int:
    return await storage.create_lifecycle_audit_row(org_id=org_id, action=action, triggered_by=triggered_by)


class _CoreApiLifecycleAdapter:
    """Adapt :class:`CoreStorageClient` to :class:`LifecycleStorageAdapter`.

    The shared handler's protocol takes ``org_id`` (the project's
    canonical key for org-scoped operations); the storage client's
    archive primitives still call the column ``tenant_id``. Translate
    at the boundary so the rename can land here without churning every
    call site of the storage client.
    """

    def __init__(self, storage: CoreStorageClient) -> None:
        self._storage = storage

    async def archive_expired(self, *, org_id: str, fleet_id: str | None) -> int:
        return await self._storage.archive_expired(org_id, fleet_id)

    async def archive_stale(self, *, org_id: str, fleet_id: str | None) -> int:
        return await self._storage.archive_stale(org_id, fleet_id, max_weight=LIFECYCLE_STALE_ARCHIVE_WEIGHT)

    async def purge_soft_deleted(self, *, org_id: str, fleet_id: str | None, retention_days: int) -> int:
        return await self._storage.purge_soft_deleted(org_id, fleet_id, retention_days=retention_days)

    async def crystallize(self, *, org_id: str, fleet_id: str | None) -> int:
        """CAURA-657: trigger crystallization for one org.

        Returns 1 for "ran and produced a report", 0 for any of the
        skip paths: org has ``auto_crystallize_enabled=False``, or
        active-memory count is below the auto-curate threshold. The
        actual crystallization metrics live on the report row whose
        UUID ``run_crystallization`` returns.

        Honors the same gates the deleted ``lifecycle_service`` had —
        the flag and the count threshold — so disabled orgs and
        small corpora don't pay for the report row plus the
        hygiene/health checks that ``run_crystallization`` runs even
        when its own ``auto_crystallize`` parameter is False.
        """
        config = await resolve_config(None, org_id)
        if not config.auto_crystallize_enabled:
            return 0
        active = await self._storage.count_active(org_id, fleet_id)
        if active <= _CRYSTALLIZE_MIN_ACTIVE_MEMORIES:
            return 0
        # Lazy import: the crystallizer service has heavy transitive
        # deps (LLM clients, pipeline steps) we don't want loading at
        # core-api startup just for the lifecycle adapter wiring.
        from core_api.services.crystallizer_service import run_crystallization

        report_id = await run_crystallization(None, org_id, fleet_id, trigger="lifecycle")
        return 1 if report_id is not None else 0

    async def insights(self, *, org_id: str, fleet_id: str | None) -> int:
        """Lifecycle-driven insights discovery (focus='discover') for one org.

        Two gates:

        1. ``config.auto_insights_enabled`` — opt-in flag (default
           **False**, unlike crystallize / entity-link which default
           True). Each tenant must flip this explicitly because the
           reflection LLM call is expensive and not all corpuses
           benefit from periodic discovery.
        2. Activity gate — skip if no non-insight memories have been
           created since the most-recent insight memory in the same
           scope. Cheap two-query check; saves an LLM round-trip when
           the corpus hasn't grown.

        Scope: ``fleet`` when a ``fleet_id`` is supplied, else ``all``.
        Returns the number of insight memories produced (audit row's
        ``stats.insights_created``).
        """
        config = await resolve_config(None, org_id)
        if not config.auto_insights_enabled:
            return 0

        # Lazy imports — insights_service has heavy transitive deps
        # (LLM clients, embedding providers) we don't want loading at
        # core-api startup just for the lifecycle adapter wiring.
        from sqlalchemy import func, select

        from common.models.memory import Memory
        from core_api.db.session import async_session
        from core_api.services.insights_service import generate_insights

        # Single session covers both the cheap activity gate and the
        # heavier ``generate_insights`` pass — mirrors entity_link's
        # pattern of one session per adapter call, explicit commit.
        async with async_session() as db:
            scope_filter = [
                Memory.tenant_id == org_id,
                Memory.deleted_at.is_(None),
            ]
            if fleet_id:
                scope_filter.append(Memory.fleet_id == fleet_id)

            latest_non_insight = await db.scalar(
                select(func.max(Memory.created_at)).where(*scope_filter, Memory.memory_type != "insight")
            )
            if latest_non_insight is None:
                return 0

            latest_insight = await db.scalar(
                select(func.max(Memory.created_at)).where(*scope_filter, Memory.memory_type == "insight")
            )
            if latest_insight is not None and latest_non_insight <= latest_insight:
                return 0

            result = await generate_insights(
                db,
                org_id,
                focus="discover",
                scope="fleet" if fleet_id else "all",
                fleet_id=fleet_id,
            )
            await db.commit()

        return len(result.get("insight_memory_ids", []))

    async def entity_link(self, *, org_id: str, fleet_id: str | None) -> int:
        """CAURA-657: run the entity-linking pipeline for one org.

        Returns ``links_created`` from the pipeline context — directly
        usable as the audit row's ``stats.links_created`` count.
        Falls back to 0 if the pipeline failed mid-run; the row will
        be marked failure by the caller, not success.

        Honors ``auto_entity_linking_enabled`` — orgs with the flag
        off return 0 without running the LLM pipeline.
        """
        config = await resolve_config(None, org_id)
        if not config.auto_entity_linking_enabled:
            return 0
        # Lazy imports — same rationale as crystallize above.
        from core_api.db.session import async_session
        from core_api.pipeline.compositions.entity_linking import (
            build_full_entity_linking_pipeline,
        )
        from core_api.pipeline.context import PipelineContext

        async with async_session() as db:
            ctx = PipelineContext(
                db=db,
                data={
                    "tenant_id": org_id,
                    **({"fleet_id": fleet_id} if fleet_id else {}),
                },
            )
            pipeline = build_full_entity_linking_pipeline()
            await pipeline.run(ctx)
            await db.commit()
            links_created = ctx.data.get("links_created", 0)
        return int(links_created)

    async def forge_distill(self, *, org_id: str, fleet_id: str | None, run_label: str) -> int:
        """Skill Factory cron tick (SF-CR3).

        Delegates to ``core_api.services.forge.cron_handler.run_forge_cron_tick``
        which:

          1. Resolves per-tenant ``ForgeConfig`` from
             ``org_settings.skills_factory.forge.*``.
          2. Runs ``run_forge_distill`` over the configured freshness
             window (mines fresh candidates).
          3. Runs ``promote_pending_candidates`` so newly-minted
             candidates passing the 6 auto-gates land in ``staged``
             in the SAME tick.

        Returns ``candidates_written + promoted`` so the
        lifecycle_audit row's ``stats_key`` reflects the meaningful
        "work done" count (a tick that writes 3 candidates of which
        2 promote returns 5).

        The shared handler's ``has_recent_lifecycle_success``
        dedup-window check (Phase 0's ``_PIPELINE_DEDUP_WINDOW_HOURS``)
        already protects against accidental double-ticks within the
        same window — operators can re-curl the fanout endpoint
        without worrying about duplicate Forge runs.

        ``run_label`` is passed from the event payload and forwarded
        to ``run_forge_cron_tick`` so candidate docs carry the same
        label that the cron tick stamped on the audit row. Threading
        it (rather than re-deriving from the consumer's clock) keeps
        the candidate's ``origin.run_id`` aligned with the event +
        audit row even when queue lag crosses a minute boundary.
        """
        # Lazy import — ``cron_handler`` pulls in the LLM provider chain via
        # ``common.llm`` which we don't want loading at core-api startup just
        # for the lifecycle adapter wiring.
        #
        # Fix 2 Ph5a: this path no longer opens an ``async_session()`` — the
        # whole forge tick (candidate scan, CAS status flips, session-trace
        # upsert, poison reads, outcome signals) goes through core-storage-api
        # via the storage client, each its own committed transaction
        # storage-side. ``tenant_id`` is threaded in place of the session.
        from core_api.services.forge.cron_handler import run_forge_cron_tick

        stats = await run_forge_cron_tick(
            tenant_id=org_id,
            fleet_id=fleet_id,
            run_label=run_label,
        )
        # ``stats_key='candidates_produced'`` (see lifecycle_handlers.py
        # registration), so the return value here is the COUNT that
        # populates ``stats.candidates_produced`` on the audit row.
        # Sum minted + promoted: an operator inspecting the audit row
        # sees a single "work done" number; the full breakdown lives
        # in the structured log line above.
        return int(stats.get("candidates_written", 0)) + int(stats.get("promoted", 0))

    async def has_recent_lifecycle_success(self, *, org_id: str, action: str, since_hours: int) -> bool:
        return await self._storage.has_recent_lifecycle_success(
            org_id=org_id, action=action, since_hours=since_hours
        )

    async def update_lifecycle_audit_row(
        self,
        audit_id: int,
        *,
        status: str,
        stats: dict | None = None,
        error_message: str | None = None,
    ) -> None:
        await self._storage.update_lifecycle_audit_row(
            audit_id, status=status, stats=stats, error_message=error_message
        )


def make_storage_adapter(storage: CoreStorageClient) -> _CoreApiLifecycleAdapter:
    """One adapter, both protocols. core-api needs the union of
    archive + pipeline methods because in OSS standalone it subscribes
    to both groups; in SaaS it only subscribes to the pipeline group
    but the archive methods stay implemented and unused (they're
    cheap and let the same adapter wire either consumer set without a
    second factory).
    """
    return _CoreApiLifecycleAdapter(storage)


async def resolve_publisher_kwargs(action: str, org_id: str) -> dict:
    """Per-action settings → publisher-kwarg map. Empty for actions
    that don't read org settings. Lives in the service layer rather
    than the route so the consumer-side adapter never accidentally
    takes a settings dependency.
    """
    if action == "purge-soft-deleted":
        config = await resolve_config(None, org_id)
        return {"retention_days": config.memory_retention_days}
    if action == "forge-distill":
        # ``publish_forge_distill_request`` requires ``run_label``; the
        # cron fanout supplies a deterministic, audit-friendly value
        # so the candidate doc's ``origin.run_id`` lets an operator
        # trace any card in the inbox back to the specific cron tick
        # that minted it. UTC minute-bucket precision avoids two
        # parallel fanout dispatches (e.g. an admin re-curl during a
        # cron firing) colliding on the SAME label.
        #
        # NOTE: ``datetime.now(UTC)`` is evaluated per-tenant. A
        # fanout that spans a UTC minute boundary will stamp tenants
        # processed before :00 with one bucket and tenants after :00
        # with the next — so two tenants that "belong to the same
        # cron tick" can carry different ``run_label`` values. For
        # cross-tenant tick correlation, use the audit-row
        # ``created_at`` timestamp rather than ``run_label``.
        # Long-term fix: thread a single ``tick_ts`` from the fanout
        # endpoint into this helper so the stamp is computed once.
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M")
        return {"run_label": f"forge-cron-{org_id}-{ts}"}
    return {}
