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
    """Provision the test database once per session, the way the app does.

    Runs the Alembic migrations via ``init_database()`` — the same call the
    FastAPI lifespan hook makes — rather than ``Base.metadata.create_all``
    over a hand-maintained model import list.

    ``create_all`` diverged from the real schema in two directions that both
    produced failures unrelated to the code under test:

    * Tables with no SQLAlchemy model were never created at all. Migration
      ``019_tenant_suppression`` owns ``tenant_suppression``, which has no
      model, so every suppression test failed on a pristine database.
    * ``create_all`` only CREATEs; it never ALTERs. A database carried over
      from an earlier revision kept its old columns, so a migration that
      added one (e.g. ``agent_activity_digests.subagents``) left the suite
      failing until the database was rebuilt by hand.

    Migrating instead makes the suite depend on the migration chain that
    production uses, so a fresh database and a reused one provision
    identically.
    """
    global _schema_ready
    if _schema_ready:
        return

    from core_storage_api.database.init import init_database

    engine = create_async_engine(settings.database_url, echo=False)
    async with engine.begin() as conn:
        # pgvector must exist before the migrations that declare vector columns.
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    await engine.dispose()

    await init_database()
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
