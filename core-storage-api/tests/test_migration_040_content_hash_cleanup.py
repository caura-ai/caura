"""Migration 040's duplicate cleanup, run against actual duplicates.

The cleanup is the only part of 040 that mutates rows, and it needs its own test
for a specific reason: it went green on both local databases having soft-deleted
**zero** rows, because neither had a duplicate to find. Green there proved the
statement parses, nothing more. Against prod it will resolve 19 rows.

The tests drop the unique index, insert real duplicates, run the migration's own
``CLEANUP_SQL`` (imported, not re-typed, so a drift between the two cannot pass),
assert the outcome, and rebuild the index to prove the cleanup actually made the
table indexable.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

# The service's ``get_session``, not ``database.init``'s: this one is an
# ``asynccontextmanager`` that commits on success. ``init``'s is a FastAPI
# dependency generator and does not commit, so every write here would roll back
# and the tests would assert against an empty table.
from core_storage_api.services.postgres_service import get_session

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

# Both from conftest, which owns the ``without_content_hash_index`` fixture that
# drops and rebuilds this index — one definition of the index shape for the
# tests, so the fixture and the assertions cannot drift apart.
from tests.conftest import UQ_LIVE_CONTENT_HASH as _INDEX  # noqa: E402
from tests.conftest import UQ_LIVE_CONTENT_HASH_SQL as _INDEX_SQL  # noqa: E402
from tests.conftest import load_migration_040  # noqa: E402


def _cleanup_sql() -> str:
    """The migration's own ``CLEANUP_SQL``, via conftest's loader."""
    return load_migration_040().CLEANUP_SQL


async def _insert(
    session,
    *,
    tenant,
    fleet,
    agent,
    content_hash,
    created_at,
    metadata="null",
    row_id=None,
):
    """Insert a live memory row directly, bypassing the service layer.

    Direct SQL because the point is to create states the application refuses to
    create — duplicates, and duplicates sharing ``created_at`` exactly.

    ``row_id`` is explicit where a test needs insertion order to differ from id
    order; otherwise random, as the app's ``gen_random_uuid()`` default is.
    """
    row_id = row_id or uuid.uuid4()
    await session.execute(
        text(
            """
            INSERT INTO memories
                (id, tenant_id, fleet_id, agent_id, memory_type, content,
                 content_hash, created_at, status, metadata)
            VALUES
                (:id, :tenant, :fleet, :agent, 'fact', :content,
                 :content_hash, :created_at, 'active', CAST(:metadata AS json))
            """
        ),
        {
            "id": row_id,
            "tenant": tenant,
            "fleet": fleet,
            "agent": agent,
            "content": f"body {row_id}",
            "content_hash": content_hash,
            "created_at": created_at,
            "metadata": metadata,
        },
    )
    return row_id


async def test_cleanup_keeps_the_oldest_and_soft_deletes_the_rest(without_content_hash_index) -> None:
    suffix = uuid.uuid4().hex[:8]
    tenant, fleet, agent = f"t-040-{suffix}", f"f-040-{suffix}", f"a-040-{suffix}"
    content_hash = f"hash-040-{suffix}"

    async with get_session() as session:
        oldest = await _insert(
            session,
            tenant=tenant,
            fleet=fleet,
            agent=agent,
            content_hash=content_hash,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        # The three metadata shapes this column actually holds. JSON ``null`` is
        # the one that matters: COALESCE passes it through, and
        # ``'null'::jsonb || '{...}'::jsonb`` WRAPS both in an array rather than
        # failing — which would drop the marker and corrupt the row's metadata.
        # Reachable because SQLAlchemy's JSON type stores a Python ``None`` as
        # JSON ``null`` unless the column sets ``none_as_null``.
        sql_null = await _insert(
            session,
            tenant=tenant,
            fleet=fleet,
            agent=agent,
            content_hash=content_hash,
            created_at=datetime(2026, 2, 1, tzinfo=UTC),
            metadata=None,
        )
        json_null = await _insert(
            session,
            tenant=tenant,
            fleet=fleet,
            agent=agent,
            content_hash=content_hash,
            created_at=datetime(2026, 3, 1, tzinfo=UTC),
            metadata="null",
        )
        with_keys = await _insert(
            session,
            tenant=tenant,
            fleet=fleet,
            agent=agent,
            content_hash=content_hash,
            created_at=datetime(2026, 4, 1, tzinfo=UTC),
            metadata='{"source": "auto_chunk"}',
        )

    async with get_session() as session:
        await session.execute(text(_cleanup_sql()))

    async with get_session() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT id, deleted_at, status,
                           metadata::jsonb ->> 'deduped_by_migration' AS marker,
                           metadata::jsonb ->> 'source' AS source
                    FROM memories WHERE content_hash = :h ORDER BY created_at
                    """
                ),
                {"h": content_hash},
            )
        ).all()

    by_id = {r.id: r for r in rows}
    assert by_id[oldest].deleted_at is None, (
        "the oldest row is the one the dedup gate returns after #839 — deleting "
        "it would silently re-point every live lookup at a different row"
    )
    assert by_id[oldest].marker is None, "the survivor must not be marked as deduped"

    for row_id in (sql_null, json_null, with_keys):
        assert by_id[row_id].deleted_at is not None, "surplus row was left live"
        assert by_id[row_id].status == "deleted"
        assert by_id[row_id].marker == "040", (
            "without the marker the 19 rows this touches in prod are unfindable afterwards"
        )

    assert by_id[with_keys].source == "auto_chunk", (
        "the marker must merge into existing metadata, not replace it"
    )


async def test_cleanup_breaks_a_created_at_tie_by_id(without_content_hash_index) -> None:
    """The tie is the COMMON case, not an edge.

    ``created_at`` is ``server_default=now()``, which Postgres fixes for a whole
    transaction, and the auto-chunk path inserts all its children in one call —
    so duplicates minted that way share ``created_at`` exactly. ``ORDER BY
    created_at`` alone would leave the choice to the plan, which is the
    instability the ``id`` tie-break exists to remove.

    MANY GROUPS, not one, and that is the load-bearing part of the design.

    A single tied group cannot falsify this: which tie a sort emits first is
    plan-dependent and not controllable from SQL, so reverting the ``id``
    tie-break leaves a one-group test passing much of the time — measured, on
    this very test: 2 of 3 runs passed with the fix removed, even inserting in
    reverse id order. That is precisely the trap of a green tie-break test.

    Across 20 independent groups, an unordered tie would have to pick the lowest
    id in ALL of them to pass. Verified: reverting the tie-break fails this.
    """
    suffix = uuid.uuid4().hex[:8]
    tenant, fleet, agent = f"t-tie-{suffix}", f"f-tie-{suffix}", f"a-tie-{suffix}"
    same_instant = datetime(2026, 4, 1, 12, tzinfo=UTC)

    groups: dict[str, list[uuid.UUID]] = {}
    async with get_session() as session:
        for group in range(20):
            content_hash = f"hash-tie-{suffix}-{group}"
            ids = sorted(uuid.uuid4() for _ in range(4))
            groups[content_hash] = ids
            # Descending id order, so "first inserted" and "lowest id" are never
            # the same row — the wrong answer cannot coincide with the right one.
            for row_id in reversed(ids):
                await _insert(
                    session,
                    tenant=tenant,
                    fleet=fleet,
                    agent=agent,
                    content_hash=content_hash,
                    created_at=same_instant,
                    row_id=row_id,
                )

    async with get_session() as session:
        await session.execute(text(_cleanup_sql()))

    async with get_session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT content_hash, id FROM memories "
                    "WHERE content_hash = ANY(:hashes) AND deleted_at IS NULL"
                ),
                {"hashes": list(groups)},
            )
        ).all()

    survivors = {r.content_hash: r.id for r in rows}
    assert len(survivors) == len(groups), (
        f"expected one survivor per group, got {len(rows)} rows across {len(survivors)} groups"
    )
    wrong = {h: (survivors[h], min(ids)) for h, ids in groups.items() if survivors[h] != min(ids)}
    assert not wrong, (
        f"{len(wrong)} of {len(groups)} groups kept a row other than the lowest "
        f"id on a created_at tie: {list(wrong.items())[:3]}"
    )


async def test_cleanup_leaves_rows_outside_the_key_alone(without_content_hash_index) -> None:
    """The guard against a cleanup that over-reaches. Same content hash, but a
    different agent and a different fleet are different keys, so all three rows
    are legitimately live and none may be touched."""
    suffix = uuid.uuid4().hex[:8]
    tenant = f"t-scope-{suffix}"
    content_hash = f"hash-scope-{suffix}"

    async with get_session() as session:
        a = await _insert(
            session,
            tenant=tenant,
            fleet=f"f-{suffix}",
            agent="agent-one",
            content_hash=content_hash,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        b = await _insert(
            session,
            tenant=tenant,
            fleet=f"f-{suffix}",
            agent="agent-two",
            content_hash=content_hash,
            created_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
        c = await _insert(
            session,
            tenant=tenant,
            fleet=f"other-{suffix}",
            agent="agent-one",
            content_hash=content_hash,
            created_at=datetime(2026, 1, 3, tzinfo=UTC),
        )
        fleetless = await _insert(
            session,
            tenant=tenant,
            fleet=None,
            agent="agent-one",
            content_hash=content_hash,
            created_at=datetime(2026, 1, 4, tzinfo=UTC),
        )

    async with get_session() as session:
        await session.execute(text(_cleanup_sql()))

    async with get_session() as session:
        live = set(
            (
                await session.execute(
                    text("SELECT id FROM memories WHERE content_hash = :h AND deleted_at IS NULL"),
                    {"h": content_hash},
                )
            )
            .scalars()
            .all()
        )

    assert live == {a, b, c, fleetless}, (
        "the cleanup touched a row outside its key: agent and fleet are part of "
        "the dedup scope, and a NULL fleet is its own group via COALESCE"
    )


async def test_an_already_soft_deleted_duplicate_is_not_re_marked(without_content_hash_index) -> None:
    """Soft-deleted rows are outside the predicate, so a re-run is a no-op on
    them. That is what makes the migration re-runnable rather than something that
    re-stamps rows on every deploy."""
    suffix = uuid.uuid4().hex[:8]
    tenant, fleet, agent = f"t-rerun-{suffix}", f"f-rerun-{suffix}", f"a-rerun-{suffix}"
    content_hash = f"hash-rerun-{suffix}"

    async with get_session() as session:
        await _insert(
            session,
            tenant=tenant,
            fleet=fleet,
            agent=agent,
            content_hash=content_hash,
            created_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        await _insert(
            session,
            tenant=tenant,
            fleet=fleet,
            agent=agent,
            content_hash=content_hash,
            created_at=datetime(2026, 5, 2, tzinfo=UTC),
        )

    async with get_session() as session:
        await session.execute(text(_cleanup_sql()))
    async with get_session() as session:
        first_pass = (
            (
                await session.execute(
                    text(
                        "SELECT deleted_at FROM memories WHERE content_hash = :h AND deleted_at IS NOT NULL"
                    ),
                    {"h": content_hash},
                )
            )
            .scalars()
            .all()
        )

    async with get_session() as session:
        await session.execute(text(_cleanup_sql()))
    async with get_session() as session:
        second_pass = (
            (
                await session.execute(
                    text(
                        "SELECT deleted_at FROM memories WHERE content_hash = :h AND deleted_at IS NOT NULL"
                    ),
                    {"h": content_hash},
                )
            )
            .scalars()
            .all()
        )

    assert len(first_pass) == 1
    assert first_pass == second_pass, "a re-run moved deleted_at on an already-resolved row"


async def test_the_index_builds_once_the_cleanup_has_run(without_content_hash_index) -> None:
    """The end-to-end claim the migration makes: after the cleanup the table is
    actually indexable. Without the cleanup this build raises."""
    suffix = uuid.uuid4().hex[:8]
    tenant, fleet, agent = f"t-build-{suffix}", f"f-build-{suffix}", f"a-build-{suffix}"
    content_hash = f"hash-build-{suffix}"

    async with get_session() as session:
        for day in (1, 2, 3):
            await _insert(
                session,
                tenant=tenant,
                fleet=fleet,
                agent=agent,
                content_hash=content_hash,
                created_at=datetime(2026, 6, day, tzinfo=UTC),
            )

    # Not built CONCURRENTLY here: this is a plain session, and CONCURRENTLY
    # cannot run inside a transaction. The migration's own build is
    # CONCURRENTLY and ``test_no_plain_create_index_on_large_tables`` enforces
    # that; what this test is about is whether the data permits the constraint.
    async with get_session() as session:
        with pytest.raises(Exception, match=r"could not create unique index|is duplicated"):
            await session.execute(text(_INDEX_SQL))

    async with get_session() as session:
        await session.execute(text(_cleanup_sql()))

    async with get_session() as session:
        await session.execute(text(_INDEX_SQL))

    async with get_session() as session:
        valid = (
            await session.execute(
                text(
                    "SELECT indisunique AND indisvalid FROM pg_index i "
                    "JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = :n"
                ),
                {"n": _INDEX},
            )
        ).scalar()
    assert valid is True


async def test_a_duplicate_insert_is_refused_with_the_winning_row_id(client) -> None:
    """The insert path end-to-end, against the real index and a real session.

    Deliberately NOT mocked. The conflict handler has to roll back and then
    re-SELECT on the same session, and ``get_session`` wraps its body in an
    explicit ``session.begin()`` — so whether a query is even legal after that
    rollback is a property of the real session, not something a mock can tell us.
    """
    suffix = uuid.uuid4().hex[:8]
    payload = {
        "tenant_id": f"t-409-{suffix}",
        "fleet_id": f"f-409-{suffix}",
        "agent_id": f"a-409-{suffix}",
        "memory_type": "fact",
        "content": f"a memory written twice {suffix}",
        "content_hash": f"hash-409-{suffix}",
    }

    first = await client.post("/api/v1/storage/memories", json=payload)
    assert first.status_code == 200, first.text
    winner_id = first.json()["id"]

    second = await client.post("/api/v1/storage/memories", json=payload)

    assert second.status_code == 409, (
        f"a duplicate insert returned {second.status_code}, not 409: {second.text}"
    )
    assert winner_id in second.json()["detail"], (
        "the 409 must name the row that already holds this content — without the "
        "id the caller cannot use the row it should have got"
    )


async def test_a_second_agent_may_write_the_same_content(client) -> None:
    """The scope guard: ``agent_id`` is in the key because two agents recording
    identical content are two independent observations."""
    suffix = uuid.uuid4().hex[:8]
    base = {
        "tenant_id": f"t-scope409-{suffix}",
        "fleet_id": f"f-scope409-{suffix}",
        "memory_type": "fact",
        "content": f"a shared observation {suffix}",
        "content_hash": f"hash-scope409-{suffix}",
    }

    first = await client.post("/api/v1/storage/memories", json={**base, "agent_id": "agent-one"})
    second = await client.post("/api/v1/storage/memories", json={**base, "agent_id": "agent-two"})

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["id"] != second.json()["id"]


async def test_a_bulk_batch_containing_a_duplicate_is_refused_with_409(client) -> None:
    """ON CONFLICT arbitrates ONE index, and this statement's is
    ``ix_memories_attempt_unique`` — so a content-hash violation is not swallowed,
    it aborts the whole multi-row INSERT. Untranslated that is a 500, which would
    mean the same duplicate answers 409 or 500 depending only on which endpoint
    the caller used.
    """
    suffix = uuid.uuid4().hex[:8]
    base = {
        "tenant_id": f"t-bulk409-{suffix}",
        "fleet_id": f"f-bulk409-{suffix}",
        "agent_id": f"a-bulk409-{suffix}",
        "memory_type": "fact",
    }
    hash_a = f"hash-bulk409-{suffix}"

    first = await client.post(
        "/api/v1/storage/memories",
        json={**base, "content": "already stored", "content_hash": hash_a},
    )
    assert first.status_code == 200, first.text

    # One fresh item and one that collides with the row above.
    resp = await client.post(
        "/api/v1/storage/memories/bulk",
        json=[
            {
                **base,
                "content": f"fresh {suffix}",
                "content_hash": f"hash-fresh-{suffix}",
                "client_request_id": f"bulk-{suffix}-0",
            },
            {
                **base,
                "content": "already stored",
                "content_hash": hash_a,
                "client_request_id": f"bulk-{suffix}-1",
            },
        ],
    )

    assert resp.status_code == 409, (
        f"a bulk batch with a duplicate returned {resp.status_code}, not 409: {resp.text}"
    )
    detail = resp.json()["detail"]
    assert "already exists" in detail
    assert "violates" not in detail.lower(), "raw driver text reached the caller"
    assert hash_a not in detail, "the offending value must not be echoed back"


async def test_a_refused_bulk_batch_writes_nothing(client) -> None:
    """The batch is one statement, so the message's "nothing in this batch was
    written" has to be true — a caller that retries must not find half of it
    already there."""
    suffix = uuid.uuid4().hex[:8]
    base = {
        "tenant_id": f"t-bulkatomic-{suffix}",
        "fleet_id": f"f-bulkatomic-{suffix}",
        "agent_id": f"a-bulkatomic-{suffix}",
        "memory_type": "fact",
    }
    hash_dup = f"hash-dup-{suffix}"
    hash_fresh = f"hash-fresh-{suffix}"

    await client.post(
        "/api/v1/storage/memories",
        json={**base, "content": "already stored", "content_hash": hash_dup},
    )

    refused = await client.post(
        "/api/v1/storage/memories/bulk",
        json=[
            {
                **base,
                "content": f"fresh {suffix}",
                "content_hash": hash_fresh,
                "client_request_id": f"atomic-{suffix}-0",
            },
            {
                **base,
                "content": "already stored",
                "content_hash": hash_dup,
                "client_request_id": f"atomic-{suffix}-1",
            },
        ],
    )
    assert refused.status_code == 409, refused.text

    # The fresh item must NOT have landed.
    found = await client.post(
        "/api/v1/storage/memories/bulk-by-content-hashes",
        json={
            "tenant_id": base["tenant_id"],
            "fleet_id": base["fleet_id"],
            "agent_id": base["agent_id"],
            "hashes": [hash_fresh],
        },
    )
    assert found.json() == {}, (
        "the aborted batch left a row behind, so the 409's claim that nothing was written is false"
    )


@pytest.mark.parametrize("fleet", ["", None], ids=["empty-string", "null"])
async def test_the_409_names_the_winner_for_either_fleet_shape(client, fleet) -> None:
    """Review round 2: the index groups by ``COALESCE(fleet_id, '')``, so a NULL
    and an empty string are the SAME key to it — but every dedup lookup branched
    on falsiness and filtered ``fleet_id IS NULL``, so none of them could see a
    row stored as ``''``.

    Reachable, not theoretical: ``fleet_id`` is ``str | None`` with no
    empty-string normalisation on the write path, so ``fleet_id: ""`` is stored
    literally. The visible symptom was worse than a wrong message — the caller
    was told "no longer live; retry the write", advice that can only 409 again.
    """
    suffix = uuid.uuid4().hex[:8]
    payload = {
        "tenant_id": f"t-fleetscope-{suffix}",
        "fleet_id": fleet,
        "agent_id": f"a-fleetscope-{suffix}",
        "memory_type": "fact",
        "content": f"body {suffix}",
        "content_hash": f"hash-fleetscope-{suffix}",
    }

    first = await client.post("/api/v1/storage/memories", json=payload)
    assert first.status_code == 200, first.text
    winner_id = first.json()["id"]
    assert first.json()["fleet_id"] == fleet, "the write path normalised fleet_id"

    second = await client.post("/api/v1/storage/memories", json=payload)

    assert second.status_code == 409, second.text
    detail = second.json()["detail"]
    assert winner_id in detail, f"the 409 did not name the live winner for fleet_id={fleet!r}: {detail!r}"
    assert "no longer live" not in detail, (
        "the lookup missed a live row and told the caller to retry — advice that can only 409 again"
    )
