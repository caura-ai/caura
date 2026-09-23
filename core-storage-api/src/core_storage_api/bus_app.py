"""Mount alongside the real storage routes and run versioned bus migrations."""

import asyncio
from contextlib import asynccontextmanager, suppress

from caura_bus_platform.routes import storage_router
from caura_bus_platform.settings import settings as collaboration_settings
from caura_bus_platform.store import Store
from caura_bus_platform.timing import TimingMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from core_storage_api.app import app
from core_storage_api.config import db_connect_args, settings


@app.exception_handler(PoolTimeout)
async def collaboration_pool_timeout(request, exc):
    if not request.url.path.startswith("/api/v1/storage/bus/"):
        raise exc
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": "1"},
        content={
            "detail": "Collaboration storage capacity is exhausted",
            "error": {
                "code": "COLLABORATION_UNAVAILABLE",
                "message": "Collaboration storage capacity is exhausted",
            },
        },
    )


def collaboration_engine(*, leader=False):
    url = settings.database_url.get_secret_value()
    pool = (
        {"poolclass": NullPool}
        if leader
        else {
            "pool_size": collaboration_settings.db_pool_size,
            "max_overflow": collaboration_settings.db_max_overflow,
            "pool_timeout": collaboration_settings.db_pool_timeout,
        }
    )
    return create_async_engine(
        url,
        connect_args=db_connect_args(url),
        **pool,
        pool_recycle=settings.db_pool_recycle,
        pool_pre_ping=True,
    )


store = (
    None
    if settings.core_storage_role == "reader"
    else Store(collaboration_engine(), leader_engine=collaboration_engine(leader=True))
)
original_lifespan = app.router.lifespan_context


@asynccontextmanager
async def lifespan(app):
    async with original_lifespan(app):
        if store is not None:
            from caura_bus_platform.wake import publish_wake

            from common.events.factory import get_event_bus

            store.publish_wake = publish_wake
            await get_event_bus().start()
            await store.migrate()
        reconciler = asyncio.create_task(store.request_reconciler()) if store is not None else None
        try:
            yield
        finally:
            if reconciler is not None:
                reconciler.cancel()
                with suppress(asyncio.CancelledError):
                    await reconciler
                try:
                    await get_event_bus().stop()
                finally:
                    await store.engine.dispose()
                    await store.leader_engine.dispose()


app.router.lifespan_context = lifespan
if store is not None:
    app.include_router(storage_router(store))
    app.add_middleware(TimingMiddleware, service="collaboration-storage")
app.openapi_schema = None
