"""Shared fixtures for core-storage-api integration tests.

Integration tests hit the FastAPI app directly via httpx ASGITransport,
backed by a real PostgreSQL database with pgvector.
"""

from __future__ import annotations

import os
import uuid

# Set test environment BEFORE any app imports touch Settings
os.environ.setdefault("TESTING", "1")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://memclaw:changeme@127.0.0.1:5432/memclaw",
)
os.environ.setdefault("LOG_LEVEL", "WARNING")

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from core_storage_api.config import settings


# ---------------------------------------------------------------------------
# Database schema setup (once per session)
# ---------------------------------------------------------------------------

_schema_ready = False


@pytest.fixture(scope="session")
async def _ensure_schema():
    """Create extensions + tables for the test database once per session."""
    global _schema_ready
    if _schema_ready:
        return

    engine = create_async_engine(settings.database_url, echo=False)

    # Import all models so metadata is populated
    import common.models.memory  # noqa: F401
    import common.models.entity  # noqa: F401
    import common.models.agent  # noqa: F401
    import common.models.audit  # noqa: F401
    import common.models.fleet  # noqa: F401
    import common.models.document  # noqa: F401
    import common.models.background_task  # noqa: F401
    import common.models.analysis_report  # noqa: F401
    from common.models.base import Base

    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)

    await engine.dispose()
    _schema_ready = True


# ---------------------------------------------------------------------------
# Async HTTP client
# ---------------------------------------------------------------------------


@pytest.fixture
async def client(_ensure_schema) -> AsyncClient:
    """Yield an async httpx client wired to the FastAPI app (no real server)."""
    from core_storage_api.app import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Tenant / fleet identifiers (unique per session to avoid collisions)
# ---------------------------------------------------------------------------

_session_suffix = uuid.uuid4().hex[:8]


@pytest.fixture
def tenant_id() -> str:
    return f"test-tenant-{_session_suffix}"


@pytest.fixture
def fleet_id() -> str:
    return f"test-fleet-{_session_suffix}"
