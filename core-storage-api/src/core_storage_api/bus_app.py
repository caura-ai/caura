"""Mount alongside the real storage routes and run versioned bus migrations."""

import asyncio
from contextlib import asynccontextmanager, suppress

from caura_bus_platform.routes import storage_router
from caura_bus_platform.settings import settings as collaboration_settings
from caura_bus_platform.store import Store
from sqlalchemy.ext.asyncio import create_async_engine

from core_storage_api.app import app
from core_storage_api.config import db_connect_args, settings


def collaboration_engine():
    url = settings.database_url.get_secret_value()
    return create_async_engine(
        url,
        connect_args=db_connect_args(url),
        pool_size=collaboration_settings.db_pool_size,
        max_overflow=collaboration_settings.db_max_overflow,
        pool_timeout=collaboration_settings.db_pool_timeout,
        pool_recycle=settings.db_pool_recycle,
        pool_pre_ping=True,
    )


store = None if settings.core_storage_role == "reader" else Store(collaboration_engine())
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


app.router.lifespan_context = lifespan
if store is not None:
    app.include_router(storage_router(store))
app.openapi_schema = None
