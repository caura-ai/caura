"""Shared fixtures for the P0 algorithmic improvements test suite.

Tests marked ``@pytest.mark.unit`` (and not ``integration``) run without any
database: the autouse storage bridge skips schema setup for them, so
``pytest -m unit`` works with no PostgreSQL reachable, and one that touches
the database anyway fails (``_keeping_off_the_database``). Every other test —
marked ``integration`` or unmarked — gets the schema, because unmarked tests
reach the database through that bridge without asking for a DB fixture. Those
require a running PostgreSQL instance with pgvector — configure via
TEST_DATABASE_URL env var or the defaults below.
"""

import asyncio
import contextlib
import inspect
import os
import sys
import uuid
import warnings
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import DEFAULT, AsyncMock, patch

# Set test-friendly defaults before any backend imports read settings.
# These can be overridden by the caller via environment variables.
_TEST_DEFAULTS = {
    "TESTING": "1",
    "EMBEDDING_PROVIDER": "fake",
    # ``none``, what ci.yml runs the suite with: a test that does not pin a
    # provider now runs the same write path here as in CI. Enrichment and
    # entity extraction are on only where a test turns them on (L-84).
    "ENTITY_EXTRACTION_PROVIDER": "none",
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
# ``~/.config/caura-keys.env`` with ``MEMCLAW_API_KEY=...`` and many  # legacy-name-floor: documents the live env alias
# rc files source it for the openclaw CLI). A leaked value flips
# ``settings.memclaw_api_key`` to truthy, which makes ``get_auth_context``  # legacy-name-floor: documents the dual-read field exercised below
# enforce the gate at Path 2 with 401s before any standalone-mode
# bypass — silently failing every test that doesn't sniff the env
# itself (e.g. test_rate_limit's auth-gated burst test, which gets all
# 401s instead of the expected 200/429 mix). Unset rather than
# setdefault — setdefault doesn't override an existing env value.
for _leaky in (
    "MEMCLAW_API_KEY",  # legacy-name-ok: rule 3 env alias
    "MEMCLAW_KEY",  # legacy-name-ok: rule 3 env alias
    "CAURA_API_KEY",
    "CAURA_KEY",
):
    os.environ.pop(_leaky, None)

# ruff: noqa: E402 — these imports MUST stay below the env defaults above;
# ``core_api.config`` reads settings at import time so ``pytest`` triggering
# test collection (which transitively imports config) must see the
# overridden env vars first.
import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import Pool

# ---------------------------------------------------------------------------
# Test database configuration
# ---------------------------------------------------------------------------

# ``caura_test``, NOT ``caura``. This suite builds its schema with
# ``Base.metadata.create_all`` and writes real rows through the service layer,
# so whatever database it points at is a test fixture, not a place to keep
# anything. Defaulting to the bare ``caura`` name aimed it squarely at the
# database a developer runs the stack against locally: a plain ``pytest tests/``
# with no environment set ran DDL on it and left rows behind under dozens of
# tenant prefixes.
#
# It also rots that database in a way that does not announce itself.
# ``create_all`` creates MISSING TABLES only — it never adds a column to a table
# that already exists — so once the models move on, the schema silently stops
# matching them and the failures surface far from the cause. Measured on a
# developer machine: 538 failures across the suite, from one absent column.
#
# ``core-storage-api/tests/conftest.py`` already defaults to its own
# ``caura_storage`` for exactly these reasons; this is the same isolation for
# the other suite. CI passes ``TEST_DATABASE_URL`` explicitly
# (.github/workflows/ci.yml), so this default is a no-op there.
#
# Create it once, alongside the database ``core-storage-api/tests/`` uses:
#     createdb caura_test && psql -d caura_test -c 'CREATE EXTENSION IF NOT EXISTS vector'
DEFAULT_TEST_DB_URL = "postgresql+asyncpg://caura:changeme@127.0.0.1:5432/caura_test"

# The database the run is pointed at. Under pytest-xdist (``pytest -n 4``) each
# worker runs against its own copy of it instead, named after the worker
# (``caura_test_gw0``, ...). Sharing one would not work: every session ends by
# deleting all ``test-tenant-`` rows, so the first worker to finish would delete
# the rows the others are still using. ``_worker_database`` makes the copy.
BASE_TEST_DB_URL = os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DB_URL)
XDIST_WORKER = os.environ.get("PYTEST_XDIST_WORKER")


def worker_database_url(url: str, worker: str | None) -> str:
    """``url`` with ``worker`` appended to its database name; ``url`` if none."""
    if not worker:
        return url
    parsed = make_url(url)
    return parsed.set(database=f"{parsed.database}_{worker}").render_as_string(
        hide_password=False
    )


TEST_DB_URL = worker_database_url(BASE_TEST_DB_URL, XDIST_WORKER)

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
    """Short unique suffix for exact-duplicate-safe content and tenant ids."""
    return uuid.uuid4().hex[:8]


def close_scheduled_coro(coro, *_args, **_kwargs):
    """``side_effect`` for a mocked scheduler: dispose of the coroutine it is handed.

    ``track_task(coro)`` receives an already-created coroutine, so a plain
    ``MagicMock`` in its place leaves that coroutine unstarted — which the
    ``filterwarnings`` gate in ``pytest.ini`` fails the run for, and whose
    comment carries the reasoning.

    Using an ``AsyncMock`` instead does NOT help: the object being dropped is
    the argument, not the mock's return value.

    Closing walks the nest rather than only the outermost coroutine, because
    the real call is
    ``track_task(tracked_task(detect_contradictions_async(...)))`` — closing
    just the wrapper leaves the inner one unstarted, trading one dropped
    coroutine for another.

    ``iscoroutine`` guards the ``None`` case, which is live: several tests stub
    ``tracked_task`` with something that returns ``None``, so this is reached
    as ``track_task(None)``.

    Returns ``mock.DEFAULT`` so the patched mock keeps its ordinary
    ``return_value`` rather than returning ``None``. Call recording —
    ``call_count``, ``assert_called_*``, ``call_args`` — is unaffected either
    way, so that is not what ``DEFAULT`` is buying.
    """
    if inspect.iscoroutine(coro):
        _close_coroutine_tree(coro)
    return DEFAULT


def _close_coroutine_tree(coro) -> None:
    """Close ``coro``, and first any coroutine it holds as an argument.

    An unstarted coroutine still has its ``cr_frame``, whose ``f_locals`` are
    its arguments. A closed one has ``cr_frame is None``, which is what keeps
    this from revisiting anything — and ``close()`` is idempotent besides.

    Known limit, measured: only coroutines held *directly* as arguments are
    found. One inside a tuple or list local is missed and still leaks. That is
    fine for every scheduler in this codebase today — ``tracked_task``'s first
    parameter is the coroutine itself — but a future wrapper taking ``*coros``
    would need this widened rather than trusted.
    """
    frame = coro.cr_frame
    if frame is not None:
        for value in frame.f_locals.values():
            if inspect.iscoroutine(value):
                _close_coroutine_tree(value)
    coro.close()


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
    import common.models.agent
    import common.models.agent_activity_digest
    import common.models.analysis_report
    import common.models.audit
    import common.models.background_task
    import common.models.capability_usage
    import common.models.dedup_review
    import common.models.document
    import common.models.entity
    import common.models.fleet
    import common.models.memory
    import common.models.organization_settings
    import common.models.recall_log
    import common.models.skill_factory  # noqa: F401


@pytest.fixture(scope="session")
def _engine(_worker_database):
    """Create a single engine for the entire test session."""
    return create_async_engine(TEST_DB_URL, echo=False, pool_size=5)


def _quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


async def _copy_base_database_for_this_worker() -> None:
    """Recreate this xdist worker's database as a copy of the base one.

    A template copy carries everything the base has, including what only the
    Alembic chain creates (CI migrates the base before the suite runs), which
    ``create_all`` below would not add. Recreated every run, so a worker never
    inherits an older run's schema or rows. PostgreSQL copies a template only
    while no other session is connected to it, so the copies are made one at a
    time under an advisory lock, from a connection to the ``postgres`` database
    rather than to the template.
    """
    import asyncpg

    base, worker = make_url(BASE_TEST_DB_URL), make_url(TEST_DB_URL)
    conn = await asyncpg.connect(
        host=base.host,
        port=base.port or 5432,
        user=base.username,
        password=base.password,
        database="postgres",
    )
    try:
        await conn.execute("SELECT pg_advisory_lock(hashtext('caura-root-suite-copy'))")
        await conn.execute(
            f"DROP DATABASE IF EXISTS {_quoted(worker.database)} WITH (FORCE)"
        )
        await conn.execute(
            f"CREATE DATABASE {_quoted(worker.database)}"
            f" TEMPLATE {_quoted(base.database)}"
        )
    finally:
        # Ending the session releases the advisory lock.
        await conn.close()


def _session_needs_database(items) -> bool:
    return any(not _needs_no_database(item) for item in items)


@pytest.fixture(scope="session")
async def _worker_database(request):
    """Make this xdist worker's database before the worker runs any test.

    Before its first test, not on the first one that asks for the schema:
    ``unit`` tests skip ``_setup_schema``, but many still reach the database
    through calls they don't stub. A serial run never notices, because the base
    database is always there, but a worker that started on one of them found
    none (``test_mcp_doc.py``: ``database "caura_gw3" does not exist``). Every
    test gets the engine through the autouse storage bridge, so requiring this
    from ``_engine`` puts it ahead of all of them. Skipped when every selected
    test is marked database-free, so ``pytest -m unit -n 4`` makes no copy and
    needs PostgreSQL only where ``pytest -m unit`` does.
    """
    if XDIST_WORKER and _session_needs_database(request.session.items):
        await _copy_base_database_for_this_worker()


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


def _tenant_scoped_tables() -> list[str]:
    """Every table the MODELS say is tenant-scoped, child-first.

    Asking the metadata rather than maintaining a literal is the point: a table
    gains a ``tenant_id`` and this sweep covers it without anyone remembering to
    come back here. ``tests/test_conftest_cleanup.py`` pins that by comparing
    this against the metadata directly.
    """
    from common.models.base import Base

    _import_all_models()
    return [t.name for t in reversed(Base.metadata.sorted_tables) if "tenant_id" in t.c]


async def _delete_or_record(
    conn, failures: list[str], label: str, sql: str, prefix: str
) -> None:
    """Run one sweep DELETE so that its failure cannot take the rest down.

    The caller hands us a live transaction — the whole sweep runs inside a
    single ``engine.begin()`` — and in PostgreSQL a statement that errors
    aborts that transaction outright: every later DELETE then fails with
    ``InFailedSqlTransaction`` whether or not it would have worked on its own.
    So without the savepoint, one missing table costs the ENTIRE sweep, and the
    warning below names twenty broken tables instead of the one that broke.
    """
    try:
        async with conn.begin_nested():
            await conn.execute(text(sql), {"prefix": prefix})
    except Exception as exc:
        failures.append(f"{label}: {exc}")


async def purge_test_rows(conn, prefix: str) -> None:
    """DELETE every tenant-scoped row whose owner id matches ``prefix``.

    Best-effort, and the ONLY thing that ever removes rows written through the
    service layer — not a backstop behind the ``db`` fixture's rollback. Most
    tests here write via ``sc``, which commits on its own connections, so that
    rollback never sees those rows. Per-test isolation comes from the unique
    ``tenant_id``; this sweep is end-of-run cleanup, so the tables don't grow
    without bound.

    Extracted from ``_setup_schema``'s teardown so it can be exercised
    directly. ``tests/test_conftest_cleanup.py`` calls this with a prefix
    unique to itself and asserts the rows are actually gone.

    Failures are collected and WARNED about rather than swallowed. They used to
    be a bare ``pass``, which — as the docstring here conceded — made a DELETE
    naming a missing table indistinguishable from one that worked, and that is
    the only way a sweep quietly stops sweeping. Still not raised: this runs in
    session teardown, where raising would turn a cleanup problem into a failed
    run that hides whatever the suite was actually reporting.
    """
    failures: list[str] = []

    # Derived from the models, NOT hand-listed. The literal this replaced named
    # 7 of the 20 tenant-scoped tables, and its own comment conceded the rest
    # "leak the same way ... a separate cleanup" — which is the shape of every
    # such list: right when written, silently wrong once a table is added.
    # Measured on a developer database beforehand: 802 leaked rows, 794 of them
    # in the 13 tables the literal omitted.
    #
    # Two of the worst offenders are worth keeping in view, because both were
    # paid for once already. ``audit_chain_head`` holds one genesis row per
    # tenant, so a per-test tenant leaves one per TEST rather than one per run
    # (a local database reached 9,186). ``documents`` holds keystones, whose
    # listing is capped at 50 and ordered by weight, so accumulated rules
    # eventually push a freshly-written one out of a round-trip test's response
    # and it fails for reasons that look nothing like accumulated state.
    # No ``tenant_id`` of its own, so the loop below cannot reach it; it is
    # matched through the memories it points at instead.
    #
    # Ahead of the loop because the loop deletes ``memories``. Both its foreign
    # keys are ON DELETE CASCADE (verified on the model and in the database), so
    # today that cascade would remove these rows and this statement is
    # redundant either way. Running it first means the sweep does not DEPEND on
    # that: drop the cascade and this still deletes the children while their
    # parents exist, rather than failing the ``memories`` DELETE on a foreign
    # key. ``tests/test_conftest_cleanup.py`` pins the end-to-end result, so a
    # schema change that breaks the assumption fails there rather than silently
    # leaking.
    await _delete_or_record(
        conn,
        failures,
        "memory_entity_links",
        "DELETE FROM memory_entity_links WHERE memory_id IN "
        "(SELECT id FROM memories WHERE tenant_id LIKE :prefix)",
        prefix,
    )

    for table in _tenant_scoped_tables():
        await _delete_or_record(
            conn,
            failures,
            table,
            f"DELETE FROM {table} WHERE tenant_id LIKE :prefix",
            prefix,
        )

    # Keyed on ``org_id``, not ``tenant_id``, so it cannot join the loop
    # either — and adding it there would look right while doing nothing.
    #
    # Unswept it is the most expensive leak here. Every test that opts a tenant
    # into a feature writes a row, and the interviewer sweep enumerates EVERY
    # enabled tenant on each tick — two storage round-trips apiece,
    # sequentially. A local database reached 4,738 enabled orgs, which put the
    # sweep in ``test_schedule_sweep_processes_pending_jobs`` at 16s and that
    # one test at 48s, growing with every run. CI never sees it: its Postgres
    # is a fresh service container per run, so this is precisely the class of
    # failure that reproduces only on a developer machine and reads as
    # flakiness rather than as accumulated state.
    await _delete_or_record(
        conn,
        failures,
        "organization_settings",
        "DELETE FROM organization_settings WHERE org_id LIKE :prefix",
        prefix,
    )

    # Keyed on ``org_id`` too, and leaks faster than the table above: it is
    # append-only, one row per settings CHANGE rather than one per org, so a
    # test that writes settings twice leaves two. The same local database
    # carried 8,676 of these against 8,238 settings rows.
    await _delete_or_record(
        conn,
        failures,
        "organization_settings_audit",
        "DELETE FROM organization_settings_audit WHERE org_id LIKE :prefix",
        prefix,
    )

    if failures:
        warnings.warn(
            "purge_test_rows could not clean "
            f"{len(failures)} table(s); rows are accumulating in the test "
            "database: " + "; ".join(failures),
            RuntimeWarning,
            stacklevel=2,
        )


# ---------------------------------------------------------------------------
# Storage client ASGI bridge (routes httpx calls to core-storage-api in-process)
# ---------------------------------------------------------------------------

_storage_asgi_http = None
_storage_sc = None
_storage_app = None


def _needs_no_database(node) -> bool:
    """Whether a test has declared itself database-free.

    Opt-out rather than opt-in: most tests carry no marker and still reach the
    database through the storage bridge, so only an explicit ``unit`` marker
    (without ``integration``) skips schema setup.
    """
    return (
        node.get_closest_marker("unit") is not None
        and node.get_closest_marker("integration") is None
    )


_REPO_ROOT = Path(__file__).resolve().parent.parent

# Where each database connection was taken from while a database-free test
# runs; None the rest of the time. See ``_keeping_off_the_database``.
_connections_taken_by_database_free_test: list[str] | None = None


@event.listens_for(Pool, "checkout")
def _record_a_database_free_test_taking_a_connection(*_args) -> None:
    if _connections_taken_by_database_free_test is not None:
        _connections_taken_by_database_free_test.append(_repo_frames(sys._getframe(1)))


def _repo_frames(frame) -> str:
    """This repo's frames from ``frame`` outward, outermost first.

    The walk continues through each greenlet's parent: async SQLAlchemy checks
    connections out on a greenlet whose own frames end at ``greenlet_spawn``,
    and the code that made the query is on the greenlet it switched from. It
    stops at the asyncio task, so a query core-api asked for over the storage
    bridge shows from the core-storage-api route down: the storage client
    sends each request from a task of its own.
    """
    import greenlet

    frames, seen, current = [], set(), greenlet.getcurrent()
    while True:
        while frame is not None and id(frame) not in seen:
            seen.add(id(frame))
            frames.append(frame)
            frame = frame.f_back
        current = current.parent
        if current is None:
            break
        frame = current.gr_frame
    lines = []
    for frame in reversed(frames):
        if frame.f_code.co_filename.startswith("<"):  # <string>, <frozen ...>
            continue
        path = Path(frame.f_code.co_filename).resolve()
        if path.is_relative_to(_REPO_ROOT) and not {".venv", "site-packages"} & set(
            path.parts
        ):
            where = f"{path.relative_to(_REPO_ROOT)}:{frame.f_lineno}"
            lines.append(f"    {where} in {frame.f_code.co_name}")
    return "\n".join(lines)


@pytest.fixture
def _schema_unless_database_free(request):
    """Set up the schema unless the test is marked database-free.

    ``_setup_schema`` cannot skip itself: it is session-scoped, so an early
    return for a first (unit) test would be cached for every later one. This
    fixture is sync on purpose — ``getfixturevalue`` on an async fixture from
    inside an async one re-enters the running event loop.

    Requested by the autouse storage bridge, this is set up before the test's
    other function-scoped fixtures and torn down after them, so what it does
    for a database-free test covers those fixtures too.
    """
    if _needs_no_database(request.node):
        with _keeping_off_the_database(request.node.nodeid, request.fixturenames):
            yield
    else:
        request.getfixturevalue("_setup_schema")
        yield


async def _never_flushed(events: list[dict]) -> None:
    raise AssertionError("a database-free test's audit queue is never flushed")


@contextlib.contextmanager
def _keeping_off_the_database(nodeid: str, fixturenames=()):
    """Run a database-free test so that touching the database fails it.

    Nothing checked this while the suite always ran against PostgreSQL, and
    303 ``unit`` tests had come to reach it, mostly through calls they never
    stubbed, so ``pytest -m unit`` failed without one. Each connection the test
    takes is recorded with where it was taken from, and the test fails with
    that. A test whose fixtures need ``_setup_schema`` fails before it starts:
    that fixture is session-scoped, set up once by whichever test asks first,
    so watching for its connections would catch it only some of the time.

    Three things every test inherits would otherwise send one there anyway:

    * Nearly every write and recall reads the tenant's settings and checks
      whether it is suppressed. Those two storage queries answer as their
      no-row branches do, for a tenant with nothing stored. Everything above
      the query, routes and core-api's handling of the answer, still runs.
    * Both answers are cached per process, so whether a test reached storage
      for them would depend on which test ran first and warmed the cache.
      Both caches start empty.
    * ``_reset_hooks`` wires the real ``log_action``, which writes straight
      to storage while no audit queue is active. A queue nothing flushes
      takes the events instead, so ``log_action`` still runs in full.
    """
    global _connections_taken_by_database_free_test
    if "_setup_schema" in fixturenames:
        pytest.fail(
            f"{nodeid} is marked unit, so it must not use the database, but a "
            "fixture it uses needs the schema (``_setup_schema``). Drop its unit "
            "marker, or that fixture.",
            pytrace=False,
        )
    from common.settings_version import version_of
    from core_api import suppression
    from core_api.services import audit_queue, organization_settings
    from core_storage_api.services.postgres_service import PostgresService

    async def no_settings_stored(self, org_id: str) -> tuple[dict, str]:
        return {}, version_of(None)

    async def not_suppressed(self, tenant_id: str) -> bool:
        return False

    suppression.reset_cache_for_testing()
    organization_settings._settings_cache.clear()
    previous_queue = audit_queue.get_audit_queue()
    audit_queue.set_audit_queue(
        audit_queue.AuditEventQueue(
            max_queue_size=10_000,
            flush_threshold=10_000,
            flush_interval_seconds=3600,
            flush_callable=_never_flushed,
        )
    )
    outer = _connections_taken_by_database_free_test  # a test of this guard nests it
    _connections_taken_by_database_free_test = taken = []
    try:
        with (
            patch.object(
                PostgresService, "organization_settings_read", no_settings_stored
            ),
            patch.object(PostgresService, "is_tenant_suppressed", not_suppressed),
        ):
            yield
    finally:
        _connections_taken_by_database_free_test = outer
        audit_queue.set_audit_queue(previous_queue)
    if taken:
        pytest.fail(
            f"{nodeid} is marked unit, so it must not use the database, but it "
            f"took {len(taken)} connection(s). Stub what it reaches, or drop "
            f"its unit marker if it needs PostgreSQL. The first was taken by:\n"
            f"{taken[0]}",
            pytrace=False,
        )


@pytest.fixture(autouse=True)
async def _patch_storage_client(_engine, _schema_unless_database_free):
    """Replace the storage client's httpx transport with an ASGI bridge.

    Routes all storage client HTTP calls to the core-storage-api FastAPI app
    in-process, so tests don't need a running server on port 8002.
    The core-storage-api session factory is pointed at the test engine.
    Neither the engine nor the bridge connects until a query runs, so a
    database-free test gets the bridge without needing PostgreSQL.
    """
    global _storage_asgi_http, _storage_sc, _storage_app
    import httpx
    from httpx import ASGITransport
    from sqlalchemy.ext.asyncio import async_sessionmaker

    import core_api.clients.storage_client as sc_mod
    import core_storage_api.services.postgres_service as pg_svc
    from core_storage_api.app import create_app as create_storage_app

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


@pytest.fixture(autouse=True)
async def _drain_background_tasks(_patch_storage_client):
    """Stop one test's fire-and-forget work from running during a later test.

    ``track_task`` registers every background task in
    ``core_api.tasks._background_tasks`` and nothing awaits them, while
    ``asyncio_default_test_loop_scope = session`` keeps one loop for the whole
    run — so a task scheduled by one test keeps running through the tests that
    follow it. Measured on this suite before this fixture existed: 207 distinct
    tasks outlived the test that created them, and one of them was still
    pending 746 tests later.

    The damage is not theoretical:

    * Log records land in a later test's ``caplog``. #1349 went red exactly
      this way, and #1352 and #1353 had to teach four assertions to ignore
      records they never emitted.
    * ``tracked_task``'s failure path calls ``get_storage_client()``. Fire
      that after ``_patch_storage_client`` has restored the original client
      and it memoises a REAL client into the module singleton, pointed at a
      storage server no test is running — which every later test then pays
      for in connection errors and retries.

    The dependency on ``_patch_storage_client`` is for ORDERING, not for a
    value: it makes this fixture set up second and therefore tear down FIRST,
    so tasks drained here still see the in-process ASGI bridge instead of
    reaching for a real client.

    Cancelled outright, with no grace period. Waiting was never an option —
    2 of those 207 tasks never finished at all, so an unconditional await
    hangs the run — and a bounded wait was worse than either: it spends real
    time to make completion *likely* for whichever tasks happen to be nearly
    done, which is a race dressed up as a courtesy. A test that needs its
    background work to complete must await it itself, as
    ``test_governance_bulk_inline_remediation`` does. This fixture promises
    isolation, not completion.
    """
    yield
    await drain_background_tasks()


#: How many times the drain yields for tasks that have not taken their first
#: step. Each yield starts every task created before it, so one is enough unless
#: a starting task schedules another; the bound only keeps a task that respawns
#: itself forever from hanging teardown. Whatever is still unstarted after it is
#: cancelled as before, and the ``pytest.ini`` gate reports what that drops.
_DRAIN_FIRST_STEP_ROUNDS = 10


def _not_started(task: asyncio.Task) -> bool:
    coro = task.get_coro()
    return (
        not task.done()
        and inspect.iscoroutine(coro)
        and inspect.getcoroutinestate(coro) == inspect.CORO_CREATED
    )


async def drain_background_tasks() -> None:
    """Cancel every background task still pending and wait for each to end.

    The body of ``_drain_background_tasks``, outside the fixture so a test can
    run it at a moment of its choosing (``test_background_task_isolation.py``).
    """
    from core_api.tasks import _background_tasks

    if all(task.done() for task in _background_tasks):
        return

    with contextlib.ExitStack() as stack:
        if _connections_taken_by_database_free_test is not None:
            # Each ``tracked_task`` records its own cancellation through
            # storage. Under a database-free test that record is the drain's
            # doing, not the test's, so it goes to a mock.
            from core_api.services import task_tracker

            stack.enter_context(
                patch.object(
                    task_tracker, "get_storage_client", return_value=AsyncMock()
                )
            )
        # A task cancelled before its first step has ``CancelledError`` thrown
        # in ahead of its first line: ``tracked_task`` then neither records the
        # cancellation nor awaits the coroutine it was handed, which is freed
        # unstarted and fails the run as "never awaited" (#2171, on
        # ``detect_contradictions_async``). Yielding until every task has
        # started puts each cancellation inside the code that handles it.
        # Nothing is closed or discarded, so the ``pytest.ini`` gate still sees
        # every coroutine the code itself drops.
        for _ in range(_DRAIN_FIRST_STEP_ROUNDS):
            if not any(_not_started(task) for task in _background_tasks):
                break
            await asyncio.sleep(0)
        pending = [task for task in _background_tasks if not task.done()]
        for task in pending:
            task.cancel()
        # Awaited so cancellation has actually landed before the next test
        # starts; ``Task.cancel()`` only requests it. ``return_exceptions`` so a
        # task that fails, or refuses to die politely, cannot turn an unrelated
        # test's teardown into an error.
        await asyncio.gather(*pending, return_exceptions=True)


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
async def _setup_app_db():
    """Standalone seed + audit hooks for the FastAPI app.

    core-api routes reach the DB only through the storage client (bridged
    in-process by the autouse ``_patch_storage_client``), so core-api itself
    holds no engine. Nor does this ask for the schema: the bridge sets it up
    for every test that is not database-free, so a ``unit`` test can drive
    the app through ``client`` with its storage calls stubbed.
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
    from httpx import ASGITransport, AsyncClient

    from core_api.app import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_dt(days_ago: float = 0, hours_ago: float = 0) -> datetime:
    """Create a timezone-aware datetime relative to now."""
    return datetime.now(UTC) - timedelta(days=days_ago, hours=hours_ago)


# ---------------------------------------------------------------------------
# Platform hooks (standalone mode wiring for integration tests)
# ---------------------------------------------------------------------------


# Re-export MCP-handler unit-test helpers so the `mcp_env` fixture is
# discoverable from any test file in this directory.
from tests._mcp_test_helpers import (  # noqa: F401
    mcp_env,
    parse_envelope,
    strip_latency,
)


@pytest.fixture(autouse=True)
def _reset_llm_provider_cache():
    """Drop cached LLM providers between tests (09/02 M-36).

    ``get_llm_provider`` memoises OpenAI-compatible providers in a module-level
    LRU so each call stops minting an unclosed ``httpx`` pool. That cache is
    process-wide by design, which in a test process means one test's provider —
    built under that test's monkeypatched credentials, env and patched classes —
    would otherwise be handed to the next test that happens to resolve the same
    configuration. The symptom is nasty: every affected test passes in
    isolation and fails in a full run.

    Same reasoning and same shape as ``_patch_storage_client`` resetting
    ``sc_mod._client`` and ``_reset_hooks`` resetting hooks: a module singleton
    that production wants and test isolation does not.
    """
    from common.llm.registry import reset_provider_cache

    reset_provider_cache()
    yield
    reset_provider_cache()


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


@pytest.fixture(autouse=True)
def _no_session_carried_over():
    """Clear the value-metering session after each test.

    ``set_session`` called from a sync test sets its ContextVar on the thread's
    own context, which every later test's task is copied from. The session
    then rode along with every write after it, each recording a value-metering
    origin through storage: ``test_value_metering_sessions.py`` left ``mcp-9``
    behind, and the database-free bulk writes that ran next reached PostgreSQL.
    """
    yield
    from core_api.session_context import set_session

    set_session(None, None)


@pytest.fixture
def task_failures(monkeypatch):
    """The ``add_task_failure`` calls a test's background tasks make, off storage.

    ``record_task_failure`` writes a ``BackgroundTaskLog`` row for each failed
    background task. A test that makes one fail on purpose asks for this, so the
    row goes to this mock instead of the database.
    """
    from core_api.services import task_tracker

    storage = AsyncMock()
    monkeypatch.setattr(task_tracker, "get_storage_client", lambda: storage)
    return storage.add_task_failure


class _RefusingStorageClient:
    """A storage client whose every call fails, as calls do when storage is down."""

    def __getattr__(self, name: str):
        async def refuse(*_args, **_kwargs):
            raise RuntimeError(f"storage is unavailable in this test ({name})")

        return refuse


@pytest.fixture
def storage_unavailable(monkeypatch):
    """Every storage call fails, through whichever module makes it.

    For a test that asserts on something decided before storage, a guard or a
    gate, and accepts any failure after it. Without this, such a test reached
    PostgreSQL through the storage bridge whenever the suite had one.
    """
    import core_api.clients.storage_client as sc_mod

    monkeypatch.setattr(sc_mod, "_client", _RefusingStorageClient())


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
