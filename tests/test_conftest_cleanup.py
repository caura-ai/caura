"""The end-of-run cleanup must actually delete what it claims to.

``purge_test_rows`` collects its failures into a warning rather than raising,
so a DELETE that cannot run still lets the session finish — which is right for
teardown and means only a test can tell you the sweep stopped sweeping.
``organization_settings`` keys on ``org_id`` rather than ``tenant_id``, which is
exactly the shape that fails silently if it is bolted onto the ``tenant_id``
loop instead of given its own statement.

Left unreclaimed those rows are not merely untidy. The interviewer schedule
sweep enumerates every enabled org on each tick, so they make it slower on
every subsequent local run — the accumulation that took one sweep test to 48s
before this was fixed.
"""

import uuid
import warnings

import pytest
from sqlalchemy import text

from tests.conftest import (
    DEFAULT_TEST_DB_URL,
    SWEEP_TENANT_PREFIX,
    _tenant_scoped_tables,
    purge_test_rows,
)


async def _count(engine, table: str, org_id: str) -> int:
    async with engine.begin() as conn:
        result = await conn.execute(
            text(f"SELECT count(*) FROM {table} WHERE org_id = :o"),
            {"o": org_id},
        )
        return int(result.scalar() or 0)


async def test_purge_reclaims_organization_settings(_engine, _setup_schema):
    # A prefix unique to this test, still inside SWEEP_TENANT_PREFIX so the
    # real end-of-run sweep would reclaim it too. Purging by this narrow
    # prefix rather than the session-wide one keeps the call from deleting
    # rows other tests in this session are still using.
    prefix = f"{SWEEP_TENANT_PREFIX}purgeprobe-{uuid.uuid4().hex[:8]}"
    org_id = f"{prefix}-org"

    async with _engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO organization_settings (org_id, settings) "
                "VALUES (:o, '{}'::jsonb)"
            ),
            {"o": org_id},
        )
        # The audit trail is written by the same request that updates the
        # settings, keys on ``org_id`` the same way, and is append-only — so
        # it leaks faster than the settings row itself.
        await conn.execute(
            text(
                "INSERT INTO organization_settings_audit (org_id, diff) "
                "VALUES (:o, '{}'::jsonb)"
            ),
            {"o": org_id},
        )
    assert await _count(_engine, "organization_settings", org_id) == 1, (
        "seed row missing"
    )
    assert await _count(_engine, "organization_settings_audit", org_id) == 1, (
        "seed audit row missing"
    )

    async with _engine.begin() as conn:
        await purge_test_rows(conn, f"{prefix}%")

    assert await _count(_engine, "organization_settings", org_id) == 0, (
        "organization_settings row survived purge_test_rows — every test that "
        "opts a tenant into a feature leaves one behind, and the interviewer "
        "sweep pays for all of them on every tick"
    )
    assert await _count(_engine, "organization_settings_audit", org_id) == 0, (
        "organization_settings_audit row survived purge_test_rows — append-only "
        "and keyed on org_id, so it accumulates faster than the settings table"
    )


def test_the_sweep_covers_every_tenant_scoped_table():
    """The swept set must BE the tenant-scoped set, not a copy of it.

    This replaced a hand-written tuple that listed 7 of 20 tables and whose own
    comment conceded the rest "leak the same way ... a separate cleanup". A
    literal list is correct when written and silently wrong the next time a
    table gains a ``tenant_id`` — nothing failed, rows just accumulated (802 on
    one developer database, 794 of them in the 13 omitted tables).

    Comparing against the metadata is the only assertion that keeps working
    when someone adds table 21.
    """
    from common.models.base import Base

    swept = set(_tenant_scoped_tables())
    tenant_scoped = {t.name for t in Base.metadata.sorted_tables if "tenant_id" in t.c}

    assert swept == tenant_scoped, (
        "the sweep and the schema disagree; unswept tables leak every run: "
        f"missing={sorted(tenant_scoped - swept)} extra={sorted(swept - tenant_scoped)}"
    )
    assert len(swept) >= 20, f"expected the full tenant-scoped set, got {len(swept)}"


def test_the_sweep_deletes_children_before_parents():
    """FK order, or the DELETEs fail and the sweep warns instead of sweeping.

    ``sorted_tables`` is parent-first; the sweep reverses it. Pinned because
    dropping the ``reversed`` still passes every other test here — the failures
    are swallowed into a warning by design.
    """
    order = _tenant_scoped_tables()
    for child, parent in (("relations", "entities"), ("memory_conflicts", "memories")):
        if child in order and parent in order:
            assert order.index(child) < order.index(parent), (
                f"{child} must be deleted before {parent}"
            )


def test_the_default_database_is_not_the_one_developers_work_in():
    """``pytest tests/`` with no environment set must not touch ``caura``.

    This suite builds its schema with ``create_all`` and writes real rows, so
    the database it defaults to is a fixture. The default used to be the bare
    ``caura`` — the name the local stack itself uses — so a plain run did DDL
    on a developer's working database and left rows behind. Worse quietly:
    ``create_all`` adds missing TABLES but never adds a column to one that
    exists, so that database drifts from the models and starts failing for
    reasons nowhere near the cause (538 failures from one absent column).

    Asserted on the constant rather than on ``TEST_DB_URL``, which is whatever
    the environment says once ``TEST_DATABASE_URL`` is exported.
    """
    database = DEFAULT_TEST_DB_URL.rsplit("/", 1)[-1]
    assert database != "caura", (
        "the default points at the database the local stack uses; a bare "
        "`pytest tests/` will run create_all against a developer's own data"
    )
    assert database != "caura_storage", (
        "that is core-storage-api's database, which is built by the ALEMBIC "
        "chain — pointing create_all at it leaves tables with no "
        "alembic_version and the migrated schema gets polluted"
    )
    assert "test" in database, f"a test database should say so: {database!r}"


async def test_one_broken_table_does_not_abort_the_rest_of_the_sweep(
    _engine, _setup_schema, monkeypatch
):
    """A failing DELETE must cost its own table's rows and nothing else.

    The whole sweep runs inside one ``engine.begin()``, and in PostgreSQL a
    statement that errors aborts that transaction: without a savepoint per
    statement, every DELETE after the first failure raises
    ``InFailedSqlTransaction`` too. The rows would still be there and the
    warning would name twenty broken tables instead of the one that broke —
    pointing the next reader at the wrong twenty.

    Driven by a table that does not exist because that is the real trigger: the
    loop is derived from model metadata, so a model can name a table the
    database has not been migrated to yet.
    """
    import tests.conftest as conftest_module

    prefix = f"{SWEEP_TENANT_PREFIX}sputter-{uuid.uuid4().hex[:8]}"
    org_id = f"{prefix}-org"

    async with _engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO organization_settings (org_id, settings) "
                "VALUES (:o, '{}'::jsonb)"
            ),
            {"o": org_id},
        )
    assert await _count(_engine, "organization_settings", org_id) == 1

    # First in the loop, so everything real runs after it.
    monkeypatch.setattr(
        conftest_module,
        "_tenant_scoped_tables",
        lambda: ["no_such_table_anywhere", *_tenant_scoped_tables()],
    )

    with pytest.warns(RuntimeWarning) as recorded:
        async with _engine.begin() as conn:
            await purge_test_rows(conn, f"{prefix}%")

    assert await _count(_engine, "organization_settings", org_id) == 0, (
        "a DELETE against a missing table poisoned the transaction and took "
        "the rest of the sweep with it"
    )

    message = str(recorded[0].message)
    assert "no_such_table_anywhere" in message, message
    assert "could not clean 1 table(s)" in message, (
        f"only the broken table should be reported, got: {message}"
    )


async def test_a_memory_and_its_entity_link_are_both_swept(_engine, _setup_schema):
    """The link table has no ``tenant_id``, so it is reached through memories.

    That statement runs ahead of the loop that deletes ``memories``, and this
    pins the result rather than the ordering: seed a memory, an entity and the
    row joining them, sweep, and require all three gone with no warning.

    Worth a test of its own because the sweep currently leans on
    ``ON DELETE CASCADE`` on both of the link's foreign keys. If that is ever
    dropped, the explicit DELETE still removes the children first — but a
    future reordering that puts it back after the loop would start leaking
    silently, and nothing else here would notice.
    """
    prefix = f"{SWEEP_TENANT_PREFIX}linkprobe-{uuid.uuid4().hex[:8]}"
    tenant_id = f"{prefix}-tenant"

    async with _engine.begin() as conn:
        memory_id = (
            await conn.execute(
                text(
                    "INSERT INTO memories (tenant_id, agent_id, memory_type, content) "
                    "VALUES (:t, 'probe-agent', 'semantic', 'probe content') "
                    "RETURNING id"
                ),
                {"t": tenant_id},
            )
        ).scalar_one()
        entity_id = (
            await conn.execute(
                text(
                    "INSERT INTO entities (tenant_id, entity_type, canonical_name) "
                    "VALUES (:t, 'person', 'probe entity') RETURNING id"
                ),
                {"t": tenant_id},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO memory_entity_links (memory_id, entity_id, role) "
                "VALUES (:m, :e, 'subject')"
            ),
            {"m": memory_id, "e": entity_id},
        )

    async def _links() -> int:
        async with _engine.begin() as conn:
            result = await conn.execute(
                text("SELECT count(*) FROM memory_entity_links WHERE memory_id = :m"),
                {"m": memory_id},
            )
            return int(result.scalar() or 0)

    async def _rows(table: str) -> int:
        async with _engine.begin() as conn:
            result = await conn.execute(
                text(f"SELECT count(*) FROM {table} WHERE tenant_id = :t"),
                {"t": tenant_id},
            )
            return int(result.scalar() or 0)

    assert await _links() == 1, "seed link missing"

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        async with _engine.begin() as conn:
            await purge_test_rows(conn, f"{prefix}%")

    purge_warnings = [
        str(w.message) for w in caught if issubclass(w.category, RuntimeWarning)
    ]
    assert not purge_warnings, (
        f"the sweep could not clean a table it must clean: {purge_warnings}"
    )
    assert await _links() == 0, "memory_entity_links row survived the sweep"
    assert await _rows("memories") == 0, "memories row survived the sweep"
    assert await _rows("entities") == 0, "entities row survived the sweep"
