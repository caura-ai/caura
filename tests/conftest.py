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
    "CORE_STORAGE_SHARED_SECRET": "test-storage-secret",
    # Interviewer async submit (#665) defaults ON in prod, but the legacy
    # route tests (tests/test_api_interview.py) assert the inline path's
    # response shape (committed/memories_written/504-on-timeout), so tests
    # pin the flag OFF here; tests/test_interview_async_submit.py flips it
    # on explicitly per-test to exercise the async path.
    "INTERVIEW_ASYNC_SUBMIT": "false",
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
for _leaky in ("MEMCLAW_API_KEY", "MEMCLAW_KEY", "CAURA_API_KEY", "CAURA_KEY"):
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

# NOT what the ``tenant_id`` fixture returns — that mints a fresh id per test. This
# is one id per RUN, kept for the modules whose module-level seed helpers need a
# tenant without taking a fixture argument. None of them relies on cross-test
# sharing, so they could each mint their own; threading a tenant through those
# helpers is the follow-up that would let this go away.
# The prefix the end-of-run sweep matches on, and the only place it is written.
# The two sides that must agree — every minter here and the DELETE in
# ``_setup_schema`` — were previously coupled by a note in a docstring, which is
# what #858 cost: a file minting its own prefix leaked every row it ever wrote.
SWEEP_TENANT_PREFIX = "test-tenant-"

TENANT_ID = f"{SWEEP_TENANT_PREFIX}{uuid.uuid4().hex[:8]}"
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
    """Short unique suffix — for distinct content (409s) and distinct tenant ids."""
    return uuid.uuid4().hex[:8]


def new_tenant_id() -> str:
    """A tenant id unique to one test AND visible to the end-of-run sweep.

    The prefix is not cosmetic: ``_setup_schema`` cleans with
    ``tenant_id LIKE 'test-tenant-%'``, so a tenant minted with any other prefix
    is never reclaimed and its committed rows outlive the run. That is how #858
    happened — two interview files minted ``t-`` tenants, their job documents
    accumulated across every local run, and a cross-tenant sweep endpoint under
    test started reading other runs' residue.

    Defined here rather than repeated at call sites so the prefix and the DELETE
    that depends on it stay in one file. The ``tenant_id`` fixture is the same
    value for tests that can take a fixture; this is for the ones that mint
    several per test.
    """
    return f"{SWEEP_TENANT_PREFIX}{uid()}"


# ---------------------------------------------------------------------------
# Integration fixtures (require PostgreSQL)
# ---------------------------------------------------------------------------


def _import_all_models():
    """Import all OSS models so SQLAlchemy metadata is populated."""
    import common.models.memory  # noqa: F401
    import common.models.entity  # noqa: F401
    import common.models.agent  # noqa: F401
    import common.models.agent_activity_digest  # noqa: F401
    import common.models.audit  # noqa: F401
    import common.models.analysis_report  # noqa: F401
    import common.models.fleet  # noqa: F401
    import common.models.document  # noqa: F401
    import common.models.background_task  # noqa: F401
    import common.models.dedup_review  # noqa: F401
    import common.models.organization_settings  # noqa: F401
    import common.models.skill_factory  # noqa: F401
    import common.models.capability_usage  # noqa: F401
    import common.models.recall_log  # noqa: F401


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
        await purge_test_rows(conn, f"{SWEEP_TENANT_PREFIX}%")


async def purge_test_rows(conn, prefix: str) -> None:
    """DELETE every tenant-scoped row whose owner id matches ``prefix``.

    Extracted from ``_setup_schema``'s teardown so it can be exercised
    directly. That matters more here than it looks: every statement below
    swallows its exception, so a DELETE naming a table that does not exist,
    or filtering on a column that table does not have, is indistinguishable
    from one that worked. ``tests/test_conftest_cleanup.py`` calls this with
    a prefix unique to itself and asserts the rows are actually gone.
    """
    for table in (
        "relations",
        "entities",
        "memories",
        "audit_log",
        # One genesis row per tenant (``_audit_chain_one_tenant``), so a
        # per-test tenant leaves one per test rather than one per run. It was
        # never swept: a local DB had 9,186 ``test-tenant-`` rows here from
        # earlier runs. Other tenant-scoped tables (recall_event,
        # memory_conflicts, dedup_reviews, agents, session_traces) leak the
        # same way but per-write, so they are a separate cleanup.
        "audit_chain_head",
        "agent_activity_digests",
        # Keystones and other docs live here. Written through the API's own
        # committed transaction, so the per-test session rollback never
        # reaches them — without this they survive every run. That is not
        # only untidy: the keystone listing is capped at 50 and ordered by
        # weight, so accumulated rules eventually push a freshly-written one
        # out of the response and a round-trip test fails for reasons that
        # look nothing like accumulated state.
        "documents",
    ):
        try:
            await conn.execute(
                text(f"DELETE FROM {table} WHERE tenant_id LIKE :prefix"),
                {"prefix": prefix},
            )
        except Exception:
            # Best-effort, and the ONLY thing that ever removes rows written
            # through the service layer — not a backstop behind the ``db``
            # fixture's rollback. Most tests here write via ``sc``, which
            # commits on its own connections, so that rollback never sees
            # those rows. Per-test isolation comes from the unique
            # ``tenant_id``; this sweep is end-of-run cleanup, so the table
            # doesn't grow without bound.
            pass
    # memory_entity_links doesn't have tenant_id — clean via memory join
    try:
        await conn.execute(
            text(
                "DELETE FROM memory_entity_links WHERE memory_id IN "
                "(SELECT id FROM memories WHERE tenant_id LIKE :prefix)"
            ),
            {"prefix": prefix},
        )
    except Exception:
        pass
    # organization_settings keys on ``org_id``, not ``tenant_id``, so it
    # cannot join the loop above — and putting it there would look right
    # while doing nothing, because the failing DELETE is swallowed.
    #
    # Unswept it is the most expensive leak here. Every test that opts a
    # tenant into a feature writes a row, and the interviewer sweep
    # enumerates EVERY enabled tenant on each tick — two storage round-trips
    # apiece, sequentially. A local database reached 4,738 enabled orgs,
    # which put the sweep in ``test_schedule_sweep_processes_pending_jobs``
    # at 16s and that one test at 48s, growing with every run. CI never sees
    # it: its Postgres is a fresh service container per run, so this is
    # precisely the class of failure that reproduces only on a developer's
    # machine and reads as flakiness rather than accumulated state.
    try:
        await conn.execute(
            text("DELETE FROM organization_settings WHERE org_id LIKE :prefix"),
            {"prefix": prefix},
        )
    except Exception:
        pass
    # ``organization_settings_audit`` is keyed on ``org_id`` too, and leaks
    # faster than the table above: it is append-only, one row per settings
    # change rather than one per org, so a test that writes settings twice
    # leaves two. Same local database carried 8,676 of these against 8,238
    # settings rows.
    try:
        await conn.execute(
            text("DELETE FROM organization_settings_audit WHERE org_id LIKE :prefix"),
            {"prefix": prefix},
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Storage client ASGI bridge (routes httpx calls to core-storage-api in-process)
# ---------------------------------------------------------------------------

_storage_asgi_http = None
_storage_sc = None
_storage_app = None


@pytest.fixture(autouse=True)
async def _patch_storage_client(_engine, _setup_schema):
    """Replace the storage client's httpx transport with an ASGI bridge.

    Routes all storage client HTTP calls to the core-storage-api FastAPI app
    in-process, so tests don't need a running server on port 8002.
    The core-storage-api session factory is pointed at the test engine.
    """
    global _storage_asgi_http, _storage_sc, _storage_app
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
        _storage_app = create_storage_app()
        transport = ASGITransport(app=_storage_app)
        _storage_asgi_http = httpx.AsyncClient(
            transport=transport,
            base_url="http://test-storage:8002",
            follow_redirects=True,
        )
        _storage_sc = sc_mod.CoreStorageClient.for_testing(
            "http://test-storage:8002", _storage_asgi_http
        )

    # Try/finally so a setup-time exception between the mutation and
    # the yield (or anywhere in the test body) restores the original
    # client. Without this guard, a failing setup or a teardown error
    # would leak the ASGI-bridged client into subsequent tests via the
    # module-level ``sc_mod._client`` singleton — caused intermittent
    # cascade failures previously (audit T5).
    old_client = sc_mod._client
    sc_mod._client = _storage_sc
    try:
        yield
    finally:
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
async def storage_http(_patch_storage_client):
    """Raw httpx client bridged to the in-process core-storage-api app.

    For tests that POST malformed/raw bodies the typed storage client would
    never send (e.g. exercising a router's 422 input-validation guards).
    Depends on ``_patch_storage_client`` so the ASGI bridge + test-engine
    session factory are wired up first.
    """
    import httpx
    from httpx import ASGITransport

    # This client exists specifically for tests that address storage routes
    # directly. Keep its credentials separate from ``_storage_asgi_http``:
    # that unadorned transport is injected into ``CoreStorageClient``, so the
    # ordinary core-api path only succeeds when the production client adds
    # its own shared-secret header.
    transport = ASGITransport(app=_storage_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test-storage:8002",
        follow_redirects=True,
        headers={"X-Storage-Secret": _TEST_DEFAULTS["CORE_STORAGE_SHARED_SECRET"]},
    ) as raw_client:
        yield raw_client


@pytest.fixture
def tenant_id():
    """A tenant ID unique to THIS test.

    It used to be one constant for the whole run, which let any test's committed
    rows satisfy a later test's ``len(results) >= 1`` — passing for the wrong
    reason, or failing only when the run order changed. Nothing removes those rows
    mid-run; see the sweep in ``_setup_schema`` for why.

    Keep the ``test-tenant-`` prefix: that sweep matches on it — see
    :func:`new_tenant_id`, which owns it.
    """
    return new_tenant_id()


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
async def _setup_app_db(_setup_schema):
    """Standalone seed + audit hooks for the FastAPI app.

    Schema is created by ``_setup_schema`` on the shared test engine; core-api
    routes reach the DB only through the storage client (bridged in-process by
    the autouse ``_patch_storage_client``), so core-api itself holds no engine.
    """
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

    In production, hooks are configured at app startup (lifespan). Tests that
    exercise the services directly bypass that lifespan, so we wire hooks here
    to guarantee audit logging behaves identically to the running server. This
    is autouse because that covers nearly every test in this tree, not only the
    ones holding a ``db`` session.
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
