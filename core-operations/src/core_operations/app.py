"""FastAPI application for core-operations.

Hosts cron/scheduled background jobs that operate on OSS data
(memories, organizations, tenants). No business HTTP surface — only
``/healthz`` for Cloud Run probes.

Lifespan ordering:
1. ``configure_logging`` reads env BEFORE any module that emits log records.
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

from common.structlog_config import configure_logging
from core_operations.config import settings
from core_operations.scheduler import scheduler, seconds_until_next_utc_hour
from core_operations.tasks import (
    run_archive_expired_tick,
    run_archive_stale_tick,
    run_crystallize_tick,
    run_entity_link_tick,
    run_insights_tick,
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


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Logging is configured here rather than at module import so test
    # runners that import this module (for `app` collection) don't
    # reconfigure structlog before pytest's caplog activates.
    configure_logging(
        settings.environment,
        settings.log_level,
        json_logs=settings.log_format_json,
        log_file=settings.log_file or None,
    )

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
