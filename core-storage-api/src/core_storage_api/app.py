"""FastAPI application for core-storage-api."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from common.structlog_config import configure_logging
from core_storage_api.config import settings

# Must run before any other module-level import emits a log record —
# database.init and the routers below pull in SQLAlchemy, httpx, etc., all
# of which log at import time. Placed after `config` so we can read
# settings, but before everything else so their records hit our handler.
configure_logging(
    settings.environment,
    settings.log_level,
    json_logs=settings.log_format_json,
    log_file=settings.log_file or None,
)

from core_storage_api.database.init import get_engine, init_database
from core_storage_api.middleware import RejectWritesOnReaderMiddleware
from core_storage_api.routers import (
    agents_router,
    audit_router,
    documents_router,
    entities_router,
    fleet_router,
    health_router,
    idempotency_router,
    keystones_router,
    lifecycle_audit_router,
    memories_router,
    reports_router,
    tasks_router,
)

logger = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan manager."""
    logger.info(
        "Starting core-storage-api",
        extra={"core_storage_role": settings.core_storage_role},
    )
    await init_database()
    yield
    logger.info("Shutting down core-storage-api")
    # Don't spin up a writer engine just to tear it down — reader-role
    # services never touch the primary DB URL, and the read-pool engine
    # lives in postgres_service.py's own factory.
    if settings.core_storage_role != "reader":
        await get_engine().dispose()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="MemClaw Core Storage API",
        description=(
            "PostgreSQL CRUD service for MemClaw core tables.\n\n"
            "Provides typed CRUD operations for memories, entities, agents, "
            "documents, fleet, audit logs, and reports.\n\n"
            "**Base path:** `/api/v1/storage`"
        ),
        version="1.0.1",
        lifespan=lifespan,
        redirect_slashes=False,
    )

    # Order matters: Starlette executes middlewares in reverse registration
    # order (last added = outermost). Register the reader filter FIRST so
    # that CORS ends up outermost and its headers decorate the 405 response
    # too — otherwise browsers see a CORS failure instead of the real 405.
    if settings.core_storage_role == "reader":
        app.add_middleware(RejectWritesOnReaderMiddleware)

    # Internal service — restrict CORS to known callers only. On the
    # reader role, narrow allow_methods so CORS preflights don't
    # advertise verbs the write-reject middleware will 405 anyway.
    allowed_origins = (
        [o.strip() for o in settings.cors_origins.split(",") if o.strip()] if settings.cors_origins else []
    )
    _cors_methods = (
        ["GET", "POST"]
        if settings.core_storage_role == "reader"
        else ["GET", "POST", "PUT", "PATCH", "DELETE"]
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=False,
        allow_methods=_cors_methods,
        allow_headers=["*"],
    )

    # Health at root level for load balancer checks
    app.include_router(health_router)

    prefix = "/api/v1/storage"

    # Health also under the API prefix
    app.include_router(health_router, prefix=prefix)
    app.include_router(memories_router, prefix=prefix)
    app.include_router(entities_router, prefix=prefix)
    app.include_router(agents_router, prefix=prefix)
    app.include_router(documents_router, prefix=prefix)
    app.include_router(keystones_router, prefix=prefix)
    app.include_router(fleet_router, prefix=prefix)
    app.include_router(audit_router, prefix=prefix)
    app.include_router(reports_router, prefix=prefix)
    app.include_router(tasks_router, prefix=prefix)
    app.include_router(idempotency_router, prefix=prefix)
    app.include_router(lifecycle_audit_router, prefix=prefix)

    return app


app = create_app()
