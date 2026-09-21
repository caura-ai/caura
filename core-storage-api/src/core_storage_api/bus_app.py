"""Mount alongside the real storage routes and run versioned bus migrations."""

from contextlib import asynccontextmanager

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
            await store.migrate()
        yield


app.router.lifespan_context = lifespan
if store is not None:
    app.include_router(storage_router(store))
app.openapi_schema = None
