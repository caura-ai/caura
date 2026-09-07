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
hazard. It is NOT known to be the cycle behind the suite's intermittent
``DeadlockDetectedError`` on this table — the statement in that traceback was
the per-item upsert, which opens one transaction per row and so cannot hold
two keys at once. Its cycle more likely runs through the foreign-key locks on
the parent ``memories`` / ``entities`` rows against a concurrent UPDATE from
background enrichment, which nothing here addresses.
"""

import asyncio
import uuid

import asyncpg
import pytest

from core_storage_api.services.postgres_service import _ordered_link_rows
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
