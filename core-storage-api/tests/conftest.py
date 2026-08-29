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

    engine = create_async_engine(settings.database_url.get_secret_value(), echo=False)
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


# ---------------------------------------------------------------------------
# Duplicate content_hash — the constraint has to come off to fabricate one
# ---------------------------------------------------------------------------

UQ_LIVE_CONTENT_HASH = "uq_memories_live_content_hash"

# Mirrors migration 040's index. Rebuilt (not CONCURRENTLY — a plain session is
# in a transaction) after each test that drops it.
UQ_LIVE_CONTENT_HASH_SQL = (
    f"CREATE UNIQUE INDEX {UQ_LIVE_CONTENT_HASH} ON memories "
    "(tenant_id, COALESCE(fleet_id, ''), agent_id, content_hash) "
    "WHERE deleted_at IS NULL AND content_hash IS NOT NULL"
)


def load_migration_040():
    """Load migration 040 as a module.

    By path because the versions directory is not an importable package and the
    filename starts with a digit. Shared so the fixture below and the migration's
    own tests both execute the REAL ``CLEANUP_SQL`` — a re-typed copy in either
    place could drift from the migration and still pass.
    """
    import importlib.util
    import pathlib

    path = (
        pathlib.Path("core-storage-api/src/core_storage_api/database/migrations/versions")
        / "040_memories_content_hash_unique.py"
    )
    spec = importlib.util.spec_from_file_location("migration_040", path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def without_content_hash_index(_ensure_schema):
    """Drop migration 040's unique index for the duration of a test.

    Shared because two kinds of test need it, and both are legitimate: the
    migration's own cleanup tests, which must create the duplicates the cleanup
    resolves, and the dedup-gate tests, which assert what the LOOKUPS do when a
    pre-existing duplicate group is present. Neither state is creatable while the
    constraint stands — that is the point of the constraint.

    Teardown runs the migration's own ``CLEANUP_SQL`` before rebuilding, because
    a test that fabricated duplicates has by definition left the table
    un-indexable; without that step the rebuild raises and every such test ends
    in a teardown ERROR. Using the migration's statement rather than an ad-hoc
    DELETE also means the fixture restores the invariant the same way production
    does.

    Uses the service's transactional ``get_session`` so the DDL commits; the
    FastAPI dependency generator in ``database.init`` does not commit.
    """
    from sqlalchemy import text

    from core_storage_api.services.postgres_service import get_session

    async with get_session() as session:
        await session.execute(text(f"DROP INDEX IF EXISTS {UQ_LIVE_CONTENT_HASH}"))
    yield
    cleanup_sql = load_migration_040().CLEANUP_SQL
    async with get_session() as session:
        await session.execute(text(f"DROP INDEX IF EXISTS {UQ_LIVE_CONTENT_HASH}"))
        await session.execute(text(cleanup_sql))
        await session.execute(text(UQ_LIVE_CONTENT_HASH_SQL))
