"""FastAPI application for core-operations.

Hosts cron/scheduled background jobs that operate on OSS data
(memories, organizations, tenants). No business HTTP surface — only
``/healthz`` for Cloud Run probes.

Logging is configured at MODULE IMPORT (see below) so uvicorn's own startup
lines route through the JSON/GCP handler; the lifespan only re-routes
third-party loggers once they're all imported.

Lifespan ordering:
1. Re-route third-party loggers (uvicorn / scheduler) onto the JSON handler.
2. If ``settings.is_standalone``: skip scheduler entirely. The service runs
   as a no-op; OSS standalone deployments should not deploy this image
   at all, but the flag is a defensive short-circuit.
3. Otherwise: register cron jobs via ``scheduler.register(...)`` and call
   ``scheduler.start()``. Eleven jobs are registered unconditionally: six daily
   lifecycle ticks (``lifecycle-archive-expired``, ``lifecycle-archive-stale``,
   ``lifecycle-purge-soft-deleted``, ``lifecycle-crystallize``,
   ``lifecycle-entity-link``, ``lifecycle-insights``), each wall-clock
   aligned to its configurable UTC hour; ``agent-digest`` (daily) and
   ``agent-digest-weekly`` (weekly) for per-agent activity digests; and
   ``interviewer-schedule`` (hourly, top of hour) which queues Interviewer
   work — per-tenant settings gate actual command creation; and
   ``embedding-coverage`` (hourly, top of hour), a read-only sample that logs
   how many live memories are still unembedded; and ``lifecycle-reconcile``
   (hourly, half past), which republishes audit rows a fanout wrote but never
   published a message for, offset off the fanout hours it repairs. A twelfth,
   ``embed-backfill``, registers only when ``embed_backfill_enabled`` is set,
   because its Pub/Sub topic is Terraform-provisioned and firing into an
   unprovisioned topic would just error every night.

   Every one of those is wall-clock aligned, so every one of them fires
   once per live replica of this service unless something coordinates
   across processes. ``scheduler.set_lease`` installs that coordination
   immediately before ``start()``; see ``core_operations.lease``.
4. Shutdown cancels all running tasks and awaits their unwind.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator

from fastapi import FastAPI, HTTPException

from common.structlog_config import configure_logging, reroute_third_party_loggers
from core_operations.config import settings
from core_operations.lease import claim_tick_lease

# Configure logging at import — before the scheduler/tasks imports below AND
# before uvicorn emits its startup lines. uvicorn imports this module during
# config load, so an import-time configure_logging() reroutes uvicorn's own
# loggers onto the JSON/GCP handler before "Started server process" / "Waiting
# for application startup" are logged. Configuring in the lifespan instead left
# those two lines to fall through uvicorn's default stderr handler, where Cloud
# Logging tagged them ERROR (false errors). E402 on the imports below is ignored
# for app.py — the config call must precede any module that emits log records.
# Test-safe: pytest caplog captures via stdlib root propagation, so the scheduler
# /task caplog assertions still work (verified: full suite green) — this mirrors
# core-api, which also configures at import with no special reset fixture.
configure_logging(
    settings.environment,
    settings.log_level,
    json_logs=settings.log_format_json,
    log_file=settings.log_file or None,
)

from core_operations.scheduler import (
    scheduler,
    seconds_until_next_utc_half_past,
    seconds_until_next_utc_hour,
    seconds_until_next_utc_top_of_hour,
    seconds_until_next_utc_weekday_hour,
)
from core_operations.tasks import (
    run_agent_digest_tick,
    run_agent_digest_weekly_tick,
    run_archive_expired_tick,
    run_archive_stale_tick,
    run_crystallize_tick,
    run_embed_backfill_tick,
    run_embedding_coverage_tick,
    run_entity_link_tick,
    run_insights_tick,
    run_interviewer_schedule_tick,
    run_lifecycle_reconcile_tick,
    run_purge_soft_deleted_tick,
)

logger = logging.getLogger(__name__)


def _register_scheduled_tasks() -> None:
    # Every lifecycle op is wall-clock aligned to a fixed UTC hour via
    # delay_provider: it fires once a day at its configured hour, never at
    # startup, and never drifts from the boot time. The ``24 * 3600`` arg
    # is the nominal daily period (documentation only — the delay_provider
    # drives the actual firing). Each op gets its own registration so an
    # outage on one can't silently mask the others; their audit rows stay
    # independent. Hours are independently configurable so operators can
    # stagger the jobs; they all default to 02:00 UTC.
    def _daily_at(hour_attr: str):
        # Bind the setting lookup lazily so an operator override applied
        # before start() is still picked up, and recompute each cycle.
        return lambda: seconds_until_next_utc_hour(getattr(settings, hour_attr))

    def _weekly_at(weekday_attr: str, hour_attr: str):
        return lambda: seconds_until_next_utc_weekday_hour(
            getattr(settings, weekday_attr), getattr(settings, hour_attr)
        )

    scheduler.register(
        "lifecycle-archive-expired",
        24 * 3600,
        run_archive_expired_tick,
        delay_provider=_daily_at("lifecycle_archive_run_at_hour"),
    )
    scheduler.register(
        "lifecycle-archive-stale",
        24 * 3600,
        run_archive_stale_tick,
        delay_provider=_daily_at("lifecycle_archive_run_at_hour"),
    )
    scheduler.register(
        "lifecycle-purge-soft-deleted",
        24 * 3600,
        run_purge_soft_deleted_tick,
        delay_provider=_daily_at("lifecycle_purge_run_at_hour"),
    )
    # A72 — cadence is configurable. At the default (24) this is byte-for-byte
    # today's behaviour: one run, wall-clock aligned to
    # ``lifecycle_pipeline_run_at_hour``. Below 24 the alignment changes meaning
    # — "every N hours from the next top of hour" rather than "at 02:00" — so
    # the delay provider switches with it rather than pretending a sub-daily
    # cadence can still anchor to one hour of the day.
    _crystallize_hours = max(1, settings.lifecycle_crystallize_every_hours)
    scheduler.register(
        "lifecycle-crystallize",
        _crystallize_hours * 3600,
        run_crystallize_tick,
        delay_provider=(
            _daily_at("lifecycle_pipeline_run_at_hour")
            if _crystallize_hours >= 24
            else (lambda: seconds_until_next_utc_top_of_hour())
        ),
    )
    scheduler.register(
        "lifecycle-entity-link",
        24 * 3600,
        run_entity_link_tick,
        # Its OWN hour, not the pipeline hour crystallize uses — see
        # ``lifecycle_entity_link_run_at_hour`` for why the two were split.
        delay_provider=_daily_at("lifecycle_entity_link_run_at_hour"),
    )
    scheduler.register(
        "lifecycle-insights",
        24 * 3600,
        run_insights_tick,
        delay_provider=_daily_at("lifecycle_insights_run_at_hour"),
    )
    # Off by default until the Pub/Sub topic is provisioned — see
    # ``embed_backfill_enabled``. Registering conditionally rather than
    # letting the tick no-op internally keeps ``scheduler.is_healthy``
    # honest: an unregistered task has no runtime slot to look dead.
    if settings.embed_backfill_enabled:
        scheduler.register(
            "embed-backfill",
            24 * 3600,
            run_embed_backfill_tick,
            delay_provider=_daily_at("embed_backfill_run_at_hour"),
        )
    scheduler.register(
        "agent-digest",
        24 * 3600,
        run_agent_digest_tick,
        delay_provider=_daily_at("agent_digest_run_at_hour"),
    )
    scheduler.register(
        "agent-digest-weekly",
        7 * 24 * 3600,
        run_agent_digest_weekly_tick,
        delay_provider=_weekly_at("agent_digest_weekly_run_at_weekday", "agent_digest_weekly_run_at_hour"),
    )
    # Repairs audit rows an earlier fanout wrote but never published a
    # message for. Hourly, so a drop is repaired within the hour rather
    # than at the action's next daily run.
    #
    # Offset to half past deliberately. Every lifecycle fanout is aligned
    # to the top of its hour, so firing there would put the sweep's own
    # storage reads in the same instant as the burst it exists to clean up
    # after. Half past also sits clear of the 30-minute strand threshold,
    # so a row from the top of this hour is never young enough to be swept
    # while its consumer may still be working on it.
    scheduler.register(
        "lifecycle-reconcile",
        3600,
        run_lifecycle_reconcile_tick,
        delay_provider=lambda: seconds_until_next_utc_half_past(),
    )
    # Interviewer Phase 1: hourly queue-only tick; per-tenant period_hours
    # gates actual command creation, so opted-out tenants pay zero cost.
    scheduler.register(
        "interviewer-schedule",
        3600,
        run_interviewer_schedule_tick,
        delay_provider=lambda: seconds_until_next_utc_top_of_hour(),
    )
    # Read-only coverage sample. Registered unconditionally and NOT gated on
    # ``embed_backfill_enabled``: the count is most valuable precisely when the
    # sweep is off, since that is when nothing is draining the backlog. Hourly
    # so the curve shows whether the nightly sweep actually drains it — a daily
    # sample taken near the sweep cannot distinguish "drained" from "never grew".
    scheduler.register(
        "embedding-coverage",
        3600,
        run_embedding_coverage_tick,
        delay_provider=lambda: seconds_until_next_utc_top_of_hour(),
    )


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Re-route third-party loggers (uvicorn / scheduler libs) onto the root JSON
    # handler now that they're all imported — the import-time configure_logging()
    # pass above no-ops for libraries imported after it (uvicorn is loaded by the
    # server). Idempotent; mirrors core-api's lifespan.
    reroute_third_party_loggers()

    logger.info(
        "Starting core-operations",
        extra={"environment": settings.environment, "standalone": settings.is_standalone},
    )

    if settings.is_standalone:
        # OSS standalone deployments shouldn't deploy this image at all.
        # If we're here it's a misconfiguration — escalate so it shows up
        # in alerts rather than silently consuming a Cloud Run slot.
        logger.warning(
            "Standalone mode — scheduler disabled. core-operations is a no-op; "
            "this image should not be deployed in standalone."
        )
        yield
        logger.info("Shutting down core-operations (standalone)")
        return

    if not settings.core_api_admin_api_key:
        # Surface the misconfig at startup so operators see it
        # immediately, not after the first cron tick fires and 401s
        # against core-api hours later.
        logger.warning(
            "CORE_API_ADMIN_API_KEY is unset; every fanout POST will be "
            "unauthorised. Set the env var before the next cron interval.",
        )

    _register_scheduled_tasks()
    # Cross-process guard for the aligned ticks. Cloud Run may run more
    # than one replica of this service, and each runs its own copy of the
    # scheduler loop, so without this every wall-clock task fires once per
    # replica inside the same second. Installed before ``start()`` because
    # ``set_lease`` refuses once the scheduler is running.
    scheduler.set_lease(claim_tick_lease)
    await scheduler.start()
    logger.info(
        "Scheduler started",
        extra={"task_count": scheduler.task_count},
    )

    yield

    logger.info("Shutting down core-operations")
    await scheduler.stop()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Caura core-operations",
        description=(
            "Host for OSS cron/scheduled jobs (lifecycle, retention, etc.). "
            "No business HTTP routes — only /healthz."
        ),
        version="1.0.0",
        lifespan=lifespan,
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        # Without this gate Cloud Run/k8s would keep an instance with a
        # crashed scheduler in rotation, looking ready while silently
        # doing nothing.
        if not scheduler.is_healthy:
            raise HTTPException(status_code=503, detail="scheduler_degraded")
        return {"status": "ok"}

    return app


app = create_app()
