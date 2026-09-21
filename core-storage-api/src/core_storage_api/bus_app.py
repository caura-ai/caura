"""Mount alongside the real storage routes and run versioned bus migrations."""

from contextlib import asynccontextmanager

from caura_bus_platform.routes import storage_router
from caura_bus_platform.store import Store

from core_storage_api.app import app
from core_storage_api.database.init import get_engine

store = Store(get_engine())
original_lifespan = app.router.lifespan_context


@asynccontextmanager
async def lifespan(app):
    async with original_lifespan(app):
        await store.migrate()
        yield


app.router.lifespan_context = lifespan
app.include_router(storage_router(store))
app.openapi_schema = None
