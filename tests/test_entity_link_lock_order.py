"""``memory_entity_links`` inserts take their locks in one global order.

The comment this replaces said ``ON CONFLICT ... DO NOTHING`` was what kept
concurrent writers off ``Lock/transactionid``. It is not, and the difference
matters: the clause decides what happens once a wait resolves, not whether
there is a wait. Measured against the real database at the shape the service
uses, a second inserter of a held key blocked for as long as the first
transaction stayed open (~600ms), then proceeded.

That wait is unavoidable. A CYCLE of waits is not, and it is what turns an
ordinary write into a 500: two transactions covering an overlapping set of
pairs in different orders each take one key and then wait on the other, and
Postgres breaks the tie by killing one of them with ``DeadlockDetectedError``.

``test_opposite_insert_orders_deadlock_at_the_sql_level`` below pins that
hazard against Postgres itself — with a barrier rather than a race, so it
cannot itself become the flaky test — and the reason ``_ordered_link_rows``
exists stays checkable rather than becoming folklore. The unit tests around
it pin the ordering the helper produces, which is the part a future edit
would break.

Scope, stated because it is easy to over-read: this closes the ordering
hazard *within this table*. It is NOT the cycle behind the suite's intermittent
``DeadlockDetectedError`` here — the statement in that traceback was the
per-item upsert, which opens one transaction per row and so cannot hold two
link keys at once.

That much was right. The guess that followed it was wrong, and is corrected
below with measurements rather than replaced with another guess. It read: "its
cycle more likely runs through the foreign-key locks on the parent memories /
entities rows against a concurrent UPDATE from background enrichment". Measured
against the real database (``test_a_plain_parent_update_does_not_block_a_link_insert``):
a plain non-key ``UPDATE`` of a parent memory does not block a link insert at
all. It cannot be half of any cycle. The FK check takes ``FOR KEY SHARE``, an
ordinary ``UPDATE`` takes ``FOR NO KEY UPDATE``, and those two do not conflict
— that is the whole point of the row-lock refinement Postgres shipped in 9.3.

What a cycle against a single-row upsert does require, also measured
(``test_locking_an_entity_before_a_memory_deadlocks_a_single_row_upsert``): a
partner that locks a parent ENTITY before a parent MEMORY, at ``FOR UPDATE``
strength. One insert holds ``memories(M)`` while it reaches for
``entities(E)``, because the two FK triggers fire in constraint-OID order and
the memories constraint is older. A partner taking the same two rows in the
opposite order closes the cycle; a partner taking them in the SAME order only
blocks, which the second half of that test pins.

So the parent-row lock order is a real invariant, and today it holds by
accident in the one place that takes both: ``_PURGE_TENANT_TABLES`` and
``_PURGE_FLEET_TABLES`` list ``memories`` before ``entities``, which is why
purge cannot cycle against a link insert. ``test_purge_locks_memories_before_entities``
pins that, since nothing else does and the tuples read as arbitrary.
"""

import asyncio
import contextlib
import uuid

import asyncpg
import pytest

from core_storage_api.services.postgres_service import (
    _ordered_link_rows,
    _ordered_memory_lock_select,
)
from tests.conftest import TEST_DB_URL

# ---------------------------------------------------------------------------
# The ordering itself
# ---------------------------------------------------------------------------


def _row(mid, eid, role="subject") -> dict:
    return {"memory_id": mid, "entity_id": eid, "role": role}


def test_rows_come_back_sorted_by_the_conflict_key() -> None:
    """Insert order is the conflict key's order, whatever the caller passed."""
    mid = uuid.UUID("11111111-1111-1111-1111-111111111111")
    ids = [
        uuid.UUID("dddddddd-0000-0000-0000-000000000000"),
        uuid.UUID("aaaaaaaa-0000-0000-0000-000000000000"),
        uuid.UUID("cccccccc-0000-0000-0000-000000000000"),
        uuid.UUID("bbbbbbbb-0000-0000-0000-000000000000"),
    ]
    forward = _ordered_link_rows([_row(mid, e) for e in ids])
    reverse = _ordered_link_rows([_row(mid, e) for e in reversed(ids)])

    got = [str(r["entity_id"]) for r in forward]
    assert got == sorted(got), got
    # The property that actually prevents the cycle: two callers holding the
    # same pairs in opposite orders emit the SAME sequence.
    assert forward == reverse


def test_multi_memory_batches_order_on_both_halves_of_the_key() -> None:
    """A batch spanning memories sorts on ``(memory_id, entity_id)``.

    ``entity_discover_cross_links`` builds exactly this shape, so ordering on
    the entity alone would leave its statement unordered across memories.
    """
    m1 = uuid.UUID("11111111-1111-1111-1111-111111111111")
    m2 = uuid.UUID("22222222-2222-2222-2222-222222222222")
    e1 = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000000")
    e2 = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000000")

    scrambled = [_row(m2, e2), _row(m1, e2), _row(m2, e1), _row(m1, e1)]
    got = [
        (str(r["memory_id"]), str(r["entity_id"]))
        for r in _ordered_link_rows(scrambled)
    ]
    assert got == sorted(got), got


def test_a_repeated_key_keeps_its_first_row() -> None:
    """Dedup keeps the first occurrence — what DO NOTHING already did.

    So a caller repeating a pair with two roles still gets the first role,
    and the statement stops taking the same lock twice.
    """
    mid = uuid.UUID("11111111-1111-1111-1111-111111111111")
    eid = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000000")
    out = _ordered_link_rows([_row(mid, eid, "subject"), _row(mid, eid, "object")])
    assert out == [_row(mid, eid, "subject")]


def test_string_and_uuid_ids_sort_together() -> None:
    """Mixed id types must not order by object identity.

    The service is called with ``UUID`` objects from the pipeline and with
    strings from the REST body, so a batch can carry both. Sorting the raw
    values would raise or fall back to identity; the helper sorts on ``str``.
    """
    mid = "11111111-1111-1111-1111-111111111111"
    rows = [
        _row(mid, "cccccccc-0000-0000-0000-000000000000"),
        _row(uuid.UUID(mid), uuid.UUID("aaaaaaaa-0000-0000-0000-000000000000")),
        _row(mid, "bbbbbbbb-0000-0000-0000-000000000000"),
    ]
    got = [str(r["entity_id"]) for r in _ordered_link_rows(rows)]
    assert got == sorted(got), got


# ---------------------------------------------------------------------------
# Why the ordering is needed at all
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_opposite_insert_orders_deadlock_at_the_sql_level(_setup_schema) -> None:
    """Postgres itself, not a claim about it: opposite orders deadlock.

    Deliberately NOT routed through the service — the point is the database
    behaviour ``_ordered_link_rows`` exists to avoid, so it must not depend on
    the helper. It therefore passes with or without the ordering; the mutants
    on the helper are what hold the mitigation, and this holds the reason.

    The cycle is built rather than raced. Each writer takes one key, both wait
    on a barrier until the other has taken theirs, and only then does each
    reach for the other's key — so neither can proceed and Postgres has to
    kill one of them. Racing N pairs in opposite orders reproduces the same
    thing, but only usually, and a test that asserts a race outcome is the
    problem this file is about.
    """
    dsn = TEST_DB_URL.replace("postgresql+asyncpg://", "postgresql://")
    tenant = f"test-tenant-{uuid.uuid4().hex[:8]}"

    setup = await asyncpg.connect(dsn)
    try:
        mid = uuid.uuid4()
        await setup.execute(
            "INSERT INTO memories (id, tenant_id, agent_id, content, memory_type,"
            " status, visibility, weight, content_hash)"
            " VALUES ($1,$2,$3,$4,'fact','active','scope_team',0.5,$5)",
            mid,
            tenant,
            "agent-lockorder",
            f"a memory whose links two writers race to insert {uuid.uuid4().hex[:8]}",
            uuid.uuid4().hex,
        )
        first, second = uuid.uuid4(), uuid.uuid4()
        for i, eid in enumerate((first, second)):
            await setup.execute(
                "INSERT INTO entities (id, tenant_id, entity_type, canonical_name)"
                " VALUES ($1,$2,'person',$3)",
                eid,
                tenant,
                f"Person {i} {uuid.uuid4().hex[:6]}",
            )
    finally:
        await setup.close()

    took_first = asyncio.Event()
    took_second = asyncio.Event()

    async def writer(mine, theirs, took_mine, took_theirs) -> str:
        conn = await asyncpg.connect(dsn)
        try:
            tx = conn.transaction()
            await tx.start()
            try:
                await conn.execute(
                    "INSERT INTO memory_entity_links (memory_id, entity_id, role)"
                    " VALUES ($1,$2,'subject') ON CONFLICT DO NOTHING",
                    mid,
                    mine,
                )
                took_mine.set()
                await took_theirs.wait()
                # Both keys are now held, one by each transaction. Reaching for
                # the other's is what closes the cycle.
                await conn.execute(
                    "INSERT INTO memory_entity_links (memory_id, entity_id, role)"
                    " VALUES ($1,$2,'subject') ON CONFLICT DO NOTHING",
                    mid,
                    theirs,
                )
                await tx.commit()
                return "ok"
            except asyncpg.exceptions.DeadlockDetectedError:
                await tx.rollback()
                return "deadlock"
        finally:
            await conn.close()

    outcomes = await asyncio.gather(
        writer(first, second, took_first, took_second),
        writer(second, first, took_second, took_first),
    )
    assert "deadlock" in outcomes, (
        f"expected a deadlock from crossed insert orders, got {outcomes} — if "
        "Postgres no longer cycles here, _ordered_link_rows may no longer be "
        "load-bearing"
    )
    # Exactly one victim: the other writer must still have committed, which is
    # what makes this a deadlock rather than both writers failing.
    assert sorted(outcomes) == ["deadlock", "ok"], outcomes


# ---------------------------------------------------------------------------
# The parent-row lock order — what actually cycles, measured
# ---------------------------------------------------------------------------


def _seed_sql_memory() -> str:
    return (
        "INSERT INTO memories (id, tenant_id, agent_id, content, memory_type,"
        " status, visibility, weight, content_hash)"
        " VALUES ($1,$2,$3,$4,'fact','active','scope_team',0.5,$5)"
    )


_INSERT_LINK = (
    "INSERT INTO memory_entity_links (memory_id, entity_id, role)"
    " VALUES ($1,$2,'subject')"
    " ON CONFLICT (memory_id, entity_id) DO UPDATE"
    " SET role = memory_entity_links.role"
)


async def _seed_one_pair(dsn, tenant):
    """One memory + one entity, returned as (memory_id, entity_id)."""
    conn = await asyncpg.connect(dsn)
    try:
        mid, eid = uuid.uuid4(), uuid.uuid4()
        await conn.execute(
            _seed_sql_memory(),
            mid,
            tenant,
            "agent-lockorder",
            f"parent-lock-order probe {uuid.uuid4().hex[:8]}",
            uuid.uuid4().hex,
        )
        await conn.execute(
            "INSERT INTO entities (id, tenant_id, entity_type, canonical_name)"
            " VALUES ($1,$2,'person',$3)",
            eid,
            tenant,
            f"Person {uuid.uuid4().hex[:6]}",
        )
        return mid, eid
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_a_plain_parent_update_does_not_block_a_link_insert(
    _setup_schema,
) -> None:
    """The corrected half of this file's docstring, held by Postgres.

    An earlier version of that docstring guessed the intermittent deadlock ran
    through the FK locks on the parents "against a concurrent UPDATE from
    background enrichment". It cannot: the FK check takes ``FOR KEY SHARE`` and
    a non-key ``UPDATE`` takes ``FOR NO KEY UPDATE``, which do not conflict.

    Like ``test_opposite_insert_orders_deadlock_at_the_sql_level``, this is a
    statement about the database rather than about our code, so it holds a
    reason rather than a mitigation. If it ever fails, the docstring above is
    wrong again and the enrichment path becomes a real suspect.
    """
    dsn = TEST_DB_URL.replace("postgresql+asyncpg://", "postgresql://")
    tenant = f"test-tenant-{uuid.uuid4().hex[:8]}"
    mid, eid = await _seed_one_pair(dsn, tenant)

    holder = await asyncpg.connect(dsn)
    inserter = await asyncpg.connect(dsn)
    try:
        tx = holder.transaction()
        await tx.start()
        # Exactly what enrichment does: rewrite a non-key column, stay open.
        await holder.execute(
            "UPDATE memories SET content = content || $2 WHERE id = $1", mid, "!"
        )

        tx2 = inserter.transaction()
        await tx2.start()
        try:
            await asyncio.wait_for(
                inserter.execute(_INSERT_LINK, mid, eid), timeout=5.0
            )
        except TimeoutError:  # pragma: no cover - the failure this test exists for
            raise AssertionError(
                "a non-key UPDATE of the parent memory blocked the link insert; "
                "FOR KEY SHARE and FOR NO KEY UPDATE are supposed to be "
                "compatible, so this file's docstring needs revisiting"
            ) from None
        await tx2.rollback()
        await tx.rollback()
    finally:
        await holder.close()
        await inserter.close()


@pytest.mark.asyncio
async def test_locking_an_entity_before_a_memory_deadlocks_a_single_row_upsert(
    _setup_schema,
) -> None:
    """The actual cycle available to a ONE-ROW link upsert, and its absence.

    A single-row insert cannot hold two link keys, but it does hold two parent
    rows: the FK triggers fire in constraint-OID order, memories first, so the
    statement holds ``memories(M)`` while it reaches for ``entities(E)``.

    Both halves are asserted, because the second is what makes the first
    actionable: a partner that takes the two parents in the opposite order
    (entity, then memory) deadlocks the upsert, and a partner that takes them
    in the same order the FK checks use only blocks. The difference is the
    entire invariant — see ``test_purge_locks_memories_before_entities``.
    """
    dsn = TEST_DB_URL.replace("postgresql+asyncpg://", "postgresql://")

    async def run(entity_first: bool) -> str:
        tenant = f"test-tenant-{uuid.uuid4().hex[:8]}"
        mid, eid = await _seed_one_pair(dsn, tenant)
        partner = await asyncpg.connect(dsn)
        upserter = await asyncpg.connect(dsn)
        try:
            ptx = partner.transaction()
            await ptx.start()
            first, second = (
                ("DELETE FROM entities WHERE id = $1", eid),
                ("DELETE FROM memories WHERE id = $1", mid),
            )
            if not entity_first:
                first, second = second, first
            await partner.execute(*first)

            outcome = "ok"

            async def upsert() -> None:
                nonlocal outcome
                utx = upserter.transaction()
                await utx.start()
                try:
                    await asyncio.wait_for(
                        upserter.execute(_INSERT_LINK, mid, eid), timeout=6.0
                    )
                    await utx.rollback()
                except asyncpg.exceptions.DeadlockDetectedError:
                    outcome = "deadlock"
                    await utx.rollback()
                except TimeoutError:
                    outcome = "blocked"

            async def close_the_cycle() -> None:
                # Let the upsert reach its second FK check and park there.
                await asyncio.sleep(0.5)
                # Either side can be the victim; which one is Postgres's choice,
                # and the assertion below is about the upsert's outcome only.
                with contextlib.suppress(
                    asyncpg.exceptions.DeadlockDetectedError, TimeoutError
                ):
                    await asyncio.wait_for(partner.execute(*second), timeout=6.0)

            await asyncio.gather(upsert(), close_the_cycle())
            # Already unwound if this side was the victim.
            with contextlib.suppress(Exception):
                await ptx.rollback()
            return outcome
        finally:
            await partner.close()
            await upserter.close()

    assert await run(entity_first=True) == "deadlock", (
        "a partner locking entities before memories no longer cycles against a "
        "single-row link upsert — the parent lock order may have changed"
    )
    assert await run(entity_first=False) == "blocked", (
        "a partner locking the parents in the FK check order should only wait, "
        "not cycle; if this deadlocks, ordering the parents no longer helps"
    )


def test_purge_locks_memories_before_entities() -> None:
    """Purge takes both parent rows in one transaction, so its order matters.

    ``purge_tenant_data`` / ``purge_fleet_data`` run one DELETE per table in a
    single session, walking these tuples in order, so the tuple order IS the
    lock order. Listing ``entities`` before ``memories`` would put purge in the
    cycling order measured above, against every concurrent link insert.

    Nothing else records that, and the tuples otherwise read as a free choice —
    their own comments say "ordering is free among these" of the trailing
    group, which is true there and not true of these two.
    """
    from core_storage_api.services.postgres_service import (
        _PURGE_FLEET_TABLES,
        _PURGE_TENANT_TABLES,
    )

    for name, tables in (
        ("_PURGE_TENANT_TABLES", _PURGE_TENANT_TABLES),
        ("_PURGE_FLEET_TABLES", _PURGE_FLEET_TABLES),
    ):
        assert "memories" in tables and "entities" in tables, name
        assert tables.index("memories") < tables.index("entities"), (
            f"{name} lists entities before memories, which is the lock order "
            "measured to deadlock a concurrent link insert"
        )


# ---------------------------------------------------------------------------
# The same hazard one table up: memories locked FOR UPDATE
# ---------------------------------------------------------------------------


def test_memory_lock_select_orders_by_id() -> None:
    """``memory_redistribute`` must fix an order for its multi-row FOR UPDATE.

    ``WHERE id IN (...)`` fixes none, which lets it take two parent memories in
    the opposite order from a concurrent link insert and cycle — reproduced
    before this helper existed. The compiled statement is asserted rather than
    the Python, because dropping ``.order_by`` is the regression and it is
    invisible in behaviour until two writers overlap.
    """
    stmt = str(
        _ordered_memory_lock_select(
            [uuid.UUID("11111111-1111-1111-1111-111111111111")], "t"
        ).compile(compile_kwargs={"literal_binds": False})
    )
    normalised = " ".join(stmt.split()).lower()
    assert "order by memories.id" in normalised, stmt
    assert "for update" in normalised, stmt
    # Order before lock: the LockRows node has to sit above the Sort, or the
    # rows are locked in scan order and the ORDER BY only sorts the output.
    assert normalised.index("order by") < normalised.index("for update"), stmt


@pytest.mark.asyncio
async def test_uuid_order_is_the_same_in_postgres_and_in_the_link_helper(
    _setup_schema,
) -> None:
    """The two paths sort by the same key, which is what makes them agree.

    ``_ordered_link_rows`` sorts on ``str(id)`` in Python; the memories lock
    orders by the ``uuid`` column in Postgres. Ordering each consistently is
    not enough — they have to produce the SAME sequence, or the two writers
    still disagree and the cycle survives both mitigations.
    """
    dsn = TEST_DB_URL.replace("postgresql+asyncpg://", "postgresql://")
    ids = [uuid.uuid4() for _ in range(200)]
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch("SELECT u FROM unnest($1::uuid[]) AS u ORDER BY u", ids)
    finally:
        await conn.close()
    in_postgres = [str(r["u"]) for r in rows]
    in_helper = sorted(str(i) for i in ids)
    assert in_postgres == in_helper
