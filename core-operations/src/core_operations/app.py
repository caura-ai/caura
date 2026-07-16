"""FastAPI application for core-operations.

Hosts cron/scheduled background jobs that operate on OSS data
(memories, organizations, tenants). No business HTTP surface — only
``/healthz`` for Cloud Run probes.

Logging is configured at MODULE IMPORT (see below) so uvicorn's own startup
lines route through the JSON/GCP handler; the lifespan only re-routes
third-party loggers once they're all imported.

Lifespan ordering:
1. Re-route third-party loggers (uvicorn / scheduler) onto the JSON handler.
2. If ``settings.standalone``: skip scheduler entirely. The service runs
   as a no-op; OSS standalone deployments should not deploy this image
   at all, but the flag is a defensive short-circuit.
3. Otherwise: register cron jobs via ``scheduler.register(...)`` and call
   ``scheduler.start()``. Each registration is its own follow-up ticket
   (CAURA-655 lifecycle migration, CAURA-656 memory retention, etc.) —
   this scaffold registers nothing.
4. Shutdown cancels all running tasks and awaits their unwind.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator

from fastapi import FastAPI, HTTPException

from common.structlog_config import configure_logging, reroute_third_party_loggers
from core_operations.config import settings

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
    run_entity_link_tick,
    run_insights_tick,
    run_interviewer_schedule_tick,
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
    scheduler.register(
        "lifecycle-crystallize",
        24 * 3600,
        run_crystallize_tick,
        delay_provider=_daily_at("lifecycle_pipeline_run_at_hour"),
    )
    scheduler.register(
        "lifecycle-entity-link",
        24 * 3600,
        run_entity_link_tick,
        delay_provider=_daily_at("lifecycle_pipeline_run_at_hour"),
    )
    scheduler.register(
        "lifecycle-insights",
        24 * 3600,
        run_insights_tick,
        delay_provider=_daily_at("lifecycle_insights_run_at_hour"),
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
    # Interviewer Phase 1: hourly queue-only tick; per-tenant period_hours
    # gates actual command creation, so opted-out tenants pay zero cost.
    scheduler.register(
        "interviewer-schedule",
        3600,
        run_interviewer_schedule_tick,
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
        extra={"environment": settings.environment, "standalone": settings.standalone},
    )

    if settings.standalone:
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
        title="MemClaw core-operations",
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
