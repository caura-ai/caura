"""Mount alongside the real storage routes and run versioned bus migrations."""

import asyncio
from contextlib import asynccontextmanager, suppress

from caura_bus_platform.routes import storage_router
from caura_bus_platform.store import Store

from core_storage_api.app import app
from core_storage_api.config import settings
from core_storage_api.database.init import get_engine

store = None if settings.core_storage_role == "reader" else Store(get_engine())
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
                await get_event_bus().stop()


app.router.lifespan_context = lifespan
if store is not None:
    app.include_router(storage_router(store))
app.openapi_schema = None
