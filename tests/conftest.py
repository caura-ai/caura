"""Shared fixtures for the P0 algorithmic improvements test suite.

Unit tests (marked @pytest.mark.unit) run without any database.
Integration tests (marked @pytest.mark.integration) require a running
PostgreSQL instance with pgvector — configure via TEST_DATABASE_URL env var
or the defaults below.
"""

import os
import uuid
from datetime import datetime, timedelta, timezone

# Set test-friendly defaults before any backend imports read settings.
# These can be overridden by the caller via environment variables.
_TEST_DEFAULTS = {
    "TESTING": "1",
    "EMBEDDING_PROVIDER": "fake",
    "ENTITY_EXTRACTION_PROVIDER": "fake",
    "USE_LLM_FOR_MEMORY_CREATION": "false",
    "ADMIN_API_KEY": "test-admin-key",
    "IS_STANDALONE": "true",
    "POSTGRES_REQUIRE_SSL": "false",
    "PLATFORM_LLM_PROVIDER": "",
    "PLATFORM_EMBEDDING_PROVIDER": "",
    # F3: ``deployment_mode`` defaults to ``"inline"`` post-Phase 3
    # (OSS shape: embed + enrich on the request path, no worker fleet
    # required). Set explicitly so a future default change doesn't
    # silently break tests; flag-off / deferred-path tests override
    # this to ``"deferred"``.
    "DEPLOYMENT_MODE": "inline",
}
for _k, _v in _TEST_DEFAULTS.items():
    os.environ.setdefault(_k, _v)

# Defensively unset env vars that change auth shape and routinely leak in
# from developers' shells (the OSS plugin onboarding writes
# ``~/.config/caura-keys.env`` with ``MEMCLAW_API_KEY=...`` and many
# rc files source it for the openclaw CLI). A leaked value flips
# ``settings.memclaw_api_key`` to truthy, which makes ``get_auth_context``
# enforce the gate at Path 2 with 401s before any standalone-mode
# bypass — silently failing every test that doesn't sniff the env
# itself (e.g. test_rate_limit's auth-gated burst test, which gets all
# 401s instead of the expected 200/429 mix). Unset rather than
# setdefault — setdefault doesn't override an existing env value.
for _leaky in ("MEMCLAW_API_KEY", "MEMCLAW_KEY"):
    os.environ.pop(_leaky, None)

# ruff: noqa: E402 — these imports MUST stay below the env defaults above;
# ``core_api.config`` reads settings at import time so ``pytest`` triggering
# test collection (which transitively imports config) must see the
# overridden env vars first.
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

# ---------------------------------------------------------------------------
# Test database configuration
# ---------------------------------------------------------------------------

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://memclaw:changeme@127.0.0.1:5432/memclaw",
)

TENANT_ID = f"test-tenant-{uuid.uuid4().hex[:8]}"
FLEET_ID = "test-fleet"
AGENT_ID = "test-agent"


# ---------------------------------------------------------------------------
# Auth helpers (OSS standalone mode — admin API key only)
# ---------------------------------------------------------------------------


def get_test_auth(tenant_id: str | None = None) -> tuple[str, dict]:
    """Return (tenant_id, headers) for OSS standalone mode.

    Uses the fixed admin API key from _TEST_DEFAULTS.
    """
    if tenant_id is None:
        tenant_id = "default"
    return tenant_id, {"X-API-Key": _TEST_DEFAULTS["ADMIN_API_KEY"]}


def get_admin_headers() -> dict:
    """Return admin auth headers."""
    return {"X-API-Key": _TEST_DEFAULTS["ADMIN_API_KEY"]}


def uid() -> str:
    """Short unique suffix to avoid duplicate-content 409s across test runs."""
    return uuid.uuid4().hex[:8]


# ---------------------------------------------------------------------------
# Integration fixtures (require PostgreSQL)
# ---------------------------------------------------------------------------


def _import_all_models():
    """Import all OSS models so SQLAlchemy metadata is populated."""
    import common.models.memory  # noqa: F401
    import common.models.entity  # noqa: F401
    import common.models.agent  # noqa: F401
    import common.models.audit  # noqa: F401
    import common.models.analysis_report  # noqa: F401
    import common.models.fleet  # noqa: F401
    import common.models.document  # noqa: F401
    import common.models.background_task  # noqa: F401
    import common.models.organization_settings  # noqa: F401


@pytest.fixture(scope="session")
def _engine():
    """Create a single engine for the entire test session."""
    return create_async_engine(TEST_DB_URL, echo=False, pool_size=5)


@pytest.fixture(scope="session")
async def _setup_schema(_engine):
    """Ensure tables + extensions exist. Runs once per session."""
    from common.models.base import Base

    _import_all_models()

    async with _engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)

    yield
    # Cleanup: drop test data (but keep schema for session reuse)
    async with _engine.begin() as conn:
        for table in (
            "relations",
            "entities",
            "memories",
            "audit_log",
        ):
            try:
                await conn.execute(
                    text(f"DELETE FROM {table} WHERE tenant_id LIKE 'test-tenant-%'")
                )
            except Exception:
                pass  # Best-effort cleanup; per-test rollback is the primary isolation
        # memory_entity_links doesn't have tenant_id — clean via memory join
        try:
            await conn.execute(
                text(
                    "DELETE FROM memory_entity_links WHERE memory_id IN "
                    "(SELECT id FROM memories WHERE tenant_id LIKE 'test-tenant-%')"
                )
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Storage client ASGI bridge (routes httpx calls to core-storage-api in-process)
# ---------------------------------------------------------------------------

_storage_asgi_http = None
_storage_sc = None


@pytest.fixture(autouse=True)
async def _patch_storage_client(_engine, _setup_schema):
    """Replace the storage client's httpx transport with an ASGI bridge.

    Routes all storage client HTTP calls to the core-storage-api FastAPI app
    in-process, so tests don't need a running server on port 8002.
    The core-storage-api session factory is pointed at the test engine.
    """
    global _storage_asgi_http, _storage_sc
    import httpx
    from httpx import ASGITransport
    from core_storage_api.app import create_app as create_storage_app
    import core_api.clients.storage_client as sc_mod
    import core_storage_api.services.postgres_service as pg_svc
    from sqlalchemy.ext.asyncio import async_sessionmaker

    # Point core-storage-api at the test engine. Reader and writer
    # share the same engine in tests (no replica spun up); prod gets
    # two engines via READ_DATABASE_URL. See CAURA-591.
    if pg_svc._session_factory is None:
        pg_svc._session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    if pg_svc._read_session_factory is None:
        pg_svc._read_session_factory = async_sessionmaker(
            _engine, expire_on_commit=False
        )

    if _storage_asgi_http is None:
        storage_app = create_storage_app()
        transport = ASGITransport(app=storage_app)
        _storage_asgi_http = httpx.AsyncClient(
            transport=transport,
            base_url="http://test-storage:8002",
            follow_redirects=True,
        )
        _storage_sc = sc_mod.CoreStorageClient.for_testing(
            "http://test-storage:8002", _storage_asgi_http
        )

    old_client = sc_mod._client
    sc_mod._client = _storage_sc
    yield
    sc_mod._client = old_client


@pytest.fixture
async def db(_engine, _setup_schema) -> AsyncSession:
    """Per-test transactional session that rolls back after each test.

    Uses join_transaction_block so that session.commit() flushes data
    without committing the outer transaction — full isolation between tests.
    """
    async with _engine.connect() as conn:
        trans = await conn.begin()
        session = AsyncSession(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        yield session
        await session.close()
        await trans.rollback()


@pytest.fixture
async def sc():
    """Storage client for tests that need committed data visible across sessions.

    Use this instead of db.add() when the data needs to be visible to the
    storage client (e.g., search, dedup, entity graph operations).
    Data written via sc is committed immediately (independent sessions).
    """
    from core_api.clients.storage_client import get_storage_client

    return get_storage_client()


@pytest.fixture
def tenant_id():
    """Unique tenant ID per test module."""
    return TENANT_ID


@pytest.fixture
def fleet_id():
    return FLEET_ID


@pytest.fixture
def agent_id():
    return AGENT_ID


# ---------------------------------------------------------------------------
# HTTP API client (E2E tests)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
async def _setup_app_db():
    """Ensure the APP's database (used by FastAPI routes) has schema + seed data."""
    from core_api.db.session import engine as app_engine
    from common.models.base import Base

    _import_all_models()

    async with app_engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)

    # Initialise standalone mode so the default tenant exists
    from core_api.standalone import init_standalone

    init_standalone()

    # Wire audit hooks
    from core_api.services.audit_service import log_action
    from core_api.services.hooks import ServiceHooks, configure_hooks

    configure_hooks(ServiceHooks(audit_log=log_action))


@pytest.fixture
async def client(_setup_app_db):
    """Async HTTP client for testing FastAPI endpoints."""
    from httpx import AsyncClient, ASGITransport
    from core_api.app import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_dt(days_ago: float = 0, hours_ago: float = 0) -> datetime:
    """Create a timezone-aware datetime relative to now."""
    return datetime.now(timezone.utc) - timedelta(days=days_ago, hours=hours_ago)


# ---------------------------------------------------------------------------
# Platform hooks (standalone mode wiring for integration tests)
# ---------------------------------------------------------------------------


# Re-export MCP-handler unit-test helpers so the `mcp_env` fixture is
# discoverable from any test file in this directory.
from tests._mcp_test_helpers import mcp_env, parse_envelope, strip_latency  # noqa: E402,F401


@pytest.fixture(autouse=True)
def _reset_hooks():
    """Ensure hooks are wired for integration tests.

    In production, hooks are configured at app startup (lifespan).  Tests that
    use the ``db`` fixture bypass the app lifespan, so we wire hooks here to
    guarantee audit logging behaves identically to the running server.
    """
    from core_api.services.audit_service import log_action
    from core_api.services.hooks import ServiceHooks, configure_hooks, reset_hooks

    configure_hooks(ServiceHooks(audit_log=log_action))
    yield
    reset_hooks()


@pytest.fixture(scope="session", autouse=True)
def _disable_rate_limiter():
    """Disable the slowapi rate limiter for the whole test suite.

    Tests share the admin API key and write many memories in tight
    bursts (fixture setup, batched assertions) that would otherwise blow
    the production write limit (10/s). Tests that specifically exercise
    the rate limiter (tests/test_rate_limit.py) re-enable it locally.
    """
    from core_api.middleware.rate_limit import limiter

    prev = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = prev
