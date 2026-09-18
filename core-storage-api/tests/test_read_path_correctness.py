"""OSS audit 08/14 + 09/02 — read paths that answer a different question.

Four findings, one shape: the response looks well-formed and is wrong in a way
the caller cannot see from it.

* **M-59** — ``GET /audit-logs`` applied ``action`` / ``resource_type`` /
  ``offset`` in Python to rows SQL had already truncated with ``LIMIT``.
* **M-62** — ``entity_list`` paged with ``OFFSET``/``LIMIT`` and no ``ORDER BY``.
* **L-31** — the entity ``memory_count`` counted links to soft-deleted
  memories, while the endpoint that lists those memories excludes them.
* **L-53** — ``audit_verify_chain`` reported ``truncated`` "so the caller knows
  to paginate" and had no cursor to paginate with.
"""

from __future__ import annotations

import contextlib
import uuid

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.dialects import postgresql

import core_storage_api.services.postgres_service as ps

pytestmark = pytest.mark.asyncio

# Every router is mounted under this; the bare paths 404.
_P = "/api/v1/storage"


async def _audit(client, tenant: str, *, action: str, resource_type: str = "memory") -> None:
    r = await client.post(
        f"{_P}/audit-logs",
        json={"tenant_id": tenant, "action": action, "resource_type": resource_type},
    )
    assert r.status_code == 200, r.text


async def _captured_sql(monkeypatch, call) -> str:
    """Compile the first statement ``call`` sends to the DB, without a DB.

    Both ordering fixes are statement-shape properties: the damage only shows
    when Postgres picks a different row order between two executions, which it
    is free to do and rarely does on a small test table, so a behavioural test
    would pass against the bug almost every time.
    """
    captured: list = []

    class _Stop(Exception):
        pass

    class _Session:
        async def execute(self, stmt, *args, **kwargs):
            captured.append(stmt)
            raise _Stop

    @contextlib.asynccontextmanager
    async def _fake_session():
        yield _Session()

    monkeypatch.setattr(ps, "get_session", _fake_session)
    with contextlib.suppress(_Stop):
        await call()

    assert captured, "no statement reached session.execute"
    return str(captured[0].compile(dialect=postgresql.dialect()))


# ---------------------------------------------------------------------------
# M-59 — the audit list's filters and offset belong in SQL.
# ---------------------------------------------------------------------------


async def test_a_filter_searches_the_whole_log_not_just_the_newest_page(client):
    """The filter used to run on rows already capped by ``LIMIT``.

    Here the match is the OLDEST of six rows and ``limit=3``, so the pre-fix
    code filtered the newest three, found nothing, and returned ``[]`` — an
    empty result for a query with a match, and nothing in the response to say
    the log had not been searched.
    """
    tenant = f"t-audit-filter-{uuid.uuid4().hex[:8]}"
    await _audit(client, tenant, action="needle")
    for _ in range(5):
        await _audit(client, tenant, action="haystack")

    r = await client.get(f"{_P}/audit-logs", params={"tenant_id": tenant, "limit": 3, "action": "needle"})
    assert r.status_code == 200, r.text
    rows = r.json()
    assert [x["action"] for x in rows] == ["needle"], (
        "the filter only searched the newest page, so an older match was reported as no match"
    )


async def test_the_second_page_is_reachable(client):
    """``offset`` used to slice a list that was itself capped at ``limit``, so
    ``offset=limit`` was empty for every tenant, always — page 2 did not exist
    however many rows there were."""
    tenant = f"t-audit-page-{uuid.uuid4().hex[:8]}"
    for i in range(6):
        await _audit(client, tenant, action=f"a{i}")

    p1 = (await client.get(f"{_P}/audit-logs", params={"tenant_id": tenant, "limit": 3, "offset": 0})).json()
    p2 = (await client.get(f"{_P}/audit-logs", params={"tenant_id": tenant, "limit": 3, "offset": 3})).json()

    assert len(p1) == 3
    assert len(p2) == 3, "page 2 came back empty — offset was applied after the SQL LIMIT"
    assert {x["id"] for x in p1}.isdisjoint({x["id"] for x in p2}), "pages overlap"


async def test_paging_covers_every_row_exactly_once(client):
    """The property the two fixes exist for, asserted end to end."""
    tenant = f"t-audit-walk-{uuid.uuid4().hex[:8]}"
    for i in range(7):
        await _audit(client, tenant, action=f"a{i}")

    inserted = {
        x["id"]
        for x in (await client.get(f"{_P}/audit-logs", params={"tenant_id": tenant, "limit": 50})).json()
    }
    assert len(inserted) == 7

    seen: list[str] = []
    sizes: list[int] = []
    for off in (0, 3, 6):
        page = (
            await client.get(f"{_P}/audit-logs", params={"tenant_id": tenant, "limit": 3, "offset": off})
        ).json()
        sizes.append(len(page))
        seen.extend(x["id"] for x in page)

    assert sizes == [3, 3, 1], f"pages were not the sizes a 7-row walk implies: {sizes}"
    assert len(seen) == 7
    assert len(set(seen)) == 7, "a row was served on two different pages"
    assert set(seen) == inserted, "the walk did not return the rows that were inserted"


async def test_filter_and_offset_compose(client):
    """Both in SQL, so ``limit`` counts FILTERED rows.

    The row count has to EXCEED the limit for this to mean anything. An earlier
    version used 8 rows with ``limit=10``: the LIMIT never truncated, so the
    Python filter saw the whole log and the test passed against the bug it was
    meant to catch. Here 20 rows and ``limit=5`` put the cut inside the data.
    """
    tenant = f"t-audit-both-{uuid.uuid4().hex[:8]}"
    for _ in range(10):
        await _audit(client, tenant, action="keep")
        await _audit(client, tenant, action="drop")

    async def _page(offset: int) -> list[dict]:
        return (
            await client.get(
                f"{_P}/audit-logs",
                params={"tenant_id": tenant, "action": "keep", "limit": 5, "offset": offset},
            )
        ).json()

    first, shifted = await _page(0), await _page(1)
    assert [x["action"] for x in first] == ["keep"] * 5, (
        "a filtered page should be a full page of matches — filtering after the "
        "LIMIT yields only the matches that happened to fall in the newest 5 rows"
    )
    # ``["keep"] * 5`` alone holds whether or not OFFSET reached SQL, so it
    # tests the filter and nothing else. The offset half of "compose" is only
    # asserted by the pages actually being shifted relative to each other.
    first_ids = [x["id"] for x in first]
    shifted_ids = [x["id"] for x in shifted]
    assert shifted_ids[:4] == first_ids[1:], (
        "offset did not compose with the filter — page(offset=1) should be "
        f"page(offset=0) advanced by exactly one row:\n  offset=0 {first_ids}\n"
        f"  offset=1 {shifted_ids}"
    )
    assert shifted_ids[-1] not in set(first_ids), "offset=1 revealed no new row"


async def test_the_audit_list_breaks_ties_on_a_unique_column(monkeypatch):
    """Statement-shape, for the same reason as ``entity_list`` above.

    ``created_at`` is assigned per-request in Python and lands milliseconds
    apart, so no behavioural test on a small table can make two rows share a
    timestamp on demand — yet the docstring calls the tiebreak the thing that
    "makes OFFSET paging coherent". Without it two rows sharing a ``created_at``
    can swap between pages and be served twice or skipped.
    """
    sql = await _captured_sql(
        monkeypatch,
        lambda: ps.PostgresService().audit_list_by_tenant("t", limit=5, offset=5, action="a"),
    )
    assert "ORDER BY audit_log.created_at DESC, audit_log.id" in sql, (
        f"the audit list must break ties on a unique column:\n{sql}"
    )
    # The filters belong in the statement too, not in the route's Python.
    assert "audit_log.action" in sql, f"action did not reach SQL:\n{sql}"
    assert "LIMIT" in sql.upper() and "OFFSET" in sql.upper()


# ---------------------------------------------------------------------------
# M-62 — OFFSET without ORDER BY is not pagination.
# ---------------------------------------------------------------------------


async def test_entity_list_orders_before_it_pages(monkeypatch):
    """Statement-shape, not behaviour, and deliberately so.

    The damage — a row served on two pages while another is never served — only
    appears when Postgres chooses a different row order between two executions,
    which it is free to do but rarely does on a small test table. A behavioural
    test would therefore pass on a broken query almost every time. The presence
    of ORDER BY is the property; the plan instability is the environment's.
    """
    sql = await _captured_sql(monkeypatch, lambda: ps.PostgresService().entity_list("t", limit=10, offset=10))
    # The COLUMN, not merely the presence of a clause. ``canonical_name`` is
    # not unique, so ordering on it reintroduces exactly the page overlap this
    # fix exists to prevent — and uniqueness is the load-bearing half of the
    # argument in the fix's own comment.
    assert "ORDER BY entities.id" in sql, f"entity_list must page on a unique column:\n{sql}"
    assert "LIMIT" in sql.upper() and "OFFSET" in sql.upper()


# ---------------------------------------------------------------------------
# L-53 — a truncation signal you can actually act on.
# ---------------------------------------------------------------------------


async def _verify(client, tenant: str, **params) -> dict:
    r = await client.get(f"{_P}/audit-logs/verify", params={"tenant_id": tenant, **params})
    assert r.status_code == 200, r.text
    return r.json()


async def test_a_truncated_verification_says_where_to_resume(client):
    """The result used to announce ``truncated`` and offer nothing to resume
    from — the docstring said the caller "knows to paginate" while the method
    took no cursor at all, so a chain longer than the route's cap could never
    have its tail verified."""
    tenant = f"t-chain-cursor-{uuid.uuid4().hex[:8]}"
    for i in range(5):
        await _audit(client, tenant, action=f"a{i}")

    first = await _verify(client, tenant, limit=2)
    assert first["valid"] is True
    assert first["truncated"] is True
    assert first["next_seq"] == 3, "no cursor to continue the walk with"


async def test_walking_every_window_verifies_the_whole_chain(client):
    """Windowed verification must reach the same verdict as one pass.

    A window above genesis seeds its ``prev_hash`` from the previous row rather
    than recomputing from the start, so this is the property that makes that
    safe: walk the windows and the last one lands on the head, un-truncated.
    """
    tenant = f"t-chain-walk-{uuid.uuid4().hex[:8]}"
    for i in range(5):
        await _audit(client, tenant, action=f"a{i}")

    whole = await _verify(client, tenant, limit=100)
    assert whole["valid"] is True and whole["truncated"] is False

    seen = 0
    start, guard = 1, 0
    while True:
        guard += 1
        assert guard < 10, "the walk did not terminate"
        page = await _verify(client, tenant, limit=2, start_seq=start)
        assert page["valid"] is True, page
        seen += page["verified_count"]
        if not page["truncated"]:
            break
        start = page["next_seq"]

    assert guard >= 3, (
        "the walk finished in fewer windows than 5 rows at limit=2 requires — a "
        "verification that ignored limit and never set truncated would satisfy "
        "every other assertion here"
    )
    assert seen == whole["verified_count"] == 5
    assert page["head_seq"] == whole["head_seq"]


async def test_a_deleted_tail_is_caught_when_the_chain_is_an_exact_multiple_of_the_window(client):
    """The windowed walk must reach the SAME verdict as one full pass.

    This is the case the equivalence claim actually rests on, and the one the
    obvious test numbers miss. Two guards skip the tail-vs-head check and
    between them they cover the whole walk: the last NON-EMPTY window returns
    exactly ``limit`` rows, so ``truncated`` is set and the check is skipped;
    the caller follows ``next_seq`` into an EMPTY window, where the check is
    skipped again for being past the end. No window ever runs it, and a walk
    over a chain whose tail was deleted ends on ``valid: true``.

    The attack that reaches it is not exotic — ``DELETE FROM audit_log WHERE
    tenant_id=$1 AND seq > 100000`` leaves the head row untouched, and 100_000
    is the documented default ``limit`` on both the route and the service. A
    contiguous tail deletion leaves no ``seq_gap`` either, so the tail check is
    the only thing that catches it.
    """
    tenant = f"t-chain-exact-{uuid.uuid4().hex[:8]}"
    for i in range(4):
        await _audit(client, tenant, action=f"a{i}")

    # Cut the tail off, leaving a chain of exactly ``limit`` rows. The head
    # still remembers seq=4, which is what makes this detectable at all.
    async with ps.get_session() as session:
        await session.execute(
            sa_text("DELETE FROM audit_log WHERE tenant_id = :t AND seq > 2"),
            {"t": tenant},
        )

    whole = await _verify(client, tenant, limit=100)
    assert whole["valid"] is False, "a full pass must still catch the severed tail"
    assert whole["first_broken"]["reason"] == "tail_truncated"

    # Now walk it the way the docstring tells callers to.
    verdicts, start, guard = [], 1, 0
    while True:
        guard += 1
        assert guard < 10, "the walk did not terminate"
        page = await _verify(client, tenant, limit=2, start_seq=start)
        verdicts.append(page)
        if not page["valid"] or not page["truncated"]:
            break
        start = page["next_seq"]

    assert any(p["valid"] is False for p in verdicts), (
        "the walk concluded the chain was intact while two rows had been "
        f"deleted off its tail — a full pass reports {whole['first_broken']}, "
        f"the windowed walk reports {verdicts}"
    )
    assert verdicts[-1]["first_broken"]["reason"] == "tail_truncated"
    # And the terminal window must report the real chain length, not 0.
    assert verdicts[-1]["first_broken"]["chain_seq"] == 2


async def test_an_intact_chain_that_is_an_exact_multiple_of_the_window_still_walks_clean(client):
    """The other half of the exact-multiple case: no tampering, same shape.

    Anchoring the terminal window's tail check to row ``start_seq - 1`` is only
    correct if the HONEST walk still comes back valid — the failure mode of
    that fix is reporting tampering on every chain whose length divides evenly
    by ``limit``. Four rows at ``limit=2`` is the smallest case where the walk
    ends on an empty window rather than a short one.

    It also pins ``head_seq``: the terminal window used to report ``0`` (the
    fallback for "no rows"), so a caller reading chain length off the last page
    of the walk got zero for a four-row chain.
    """
    tenant = f"t-chain-even-{uuid.uuid4().hex[:8]}"
    for i in range(4):
        await _audit(client, tenant, action=f"a{i}")

    pages, start, guard = [], 1, 0
    while True:
        guard += 1
        assert guard < 10, "the walk did not terminate"
        page = await _verify(client, tenant, limit=2, start_seq=start)
        assert page["valid"] is True, f"honest chain reported broken: {page}"
        pages.append(page)
        if not page["truncated"]:
            break
        start = page["next_seq"]

    assert len(pages) == 3, f"expected two full windows then an empty one, got {pages}"
    assert [p["verified_count"] for p in pages] == [2, 2, 0]
    assert pages[-1]["head_seq"] == 4, (
        f"the terminal window must report the real chain head, not the no-rows fallback: {pages[-1]}"
    )


async def test_a_window_whose_predecessor_is_missing_is_refused(client):
    """Resuming from a seq whose previous row is gone must not verify against
    genesis. That would report a sound chain for one whose head had been cut
    off exactly at the resume point — the deletion a chain walk exists to
    catch."""
    tenant = f"t-chain-orphan-{uuid.uuid4().hex[:8]}"
    for i in range(3):
        await _audit(client, tenant, action=f"a{i}")

    res = await _verify(client, tenant, limit=10, start_seq=99)
    assert res["valid"] is False
    assert res["first_broken"]["reason"] == "missing_predecessor"


async def test_the_final_window_does_not_report_a_severed_tail(client):
    """An empty window past the end is how the walk terminates, not tampering.

    ``last_seq`` is 0 for an empty page while the head legitimately is not, so
    without the walked-past-end guard the last page of every paginated
    verification would report ``tail_truncated``.
    """
    tenant = f"t-chain-end-{uuid.uuid4().hex[:8]}"
    for i in range(3):
        await _audit(client, tenant, action=f"a{i}")

    res = await _verify(client, tenant, limit=10, start_seq=4)
    assert res["valid"] is True, res
    assert res["verified_count"] == 0
    assert res["truncated"] is False


async def test_a_tampered_row_is_caught_by_the_window_that_covers_it(client):
    """Windowed verification must catch what a full pass catches.

    This is the assertion the resume feature actually rests on. A window above
    genesis SEEDS ``prev_hash`` from the previous row instead of recomputing
    from the start, so an implementation that checked only ``seq`` continuity
    and linkage would look identical to this one on an intact chain — and would
    sail straight past a row whose CONTENT was rewritten. Recomputing the hash
    is the only thing that sees that, and it has to run in every window rather
    than just the first.

    Both entry points are covered: the window that straddles the tampered row
    and the one that starts on it.
    """
    tenant = f"t-chain-tamper-{uuid.uuid4().hex[:8]}"
    for i in range(5):
        await _audit(client, tenant, action=f"a{i}")

    async with ps.get_session() as session:
        await session.execute(
            sa_text("UPDATE audit_log SET action = 'TAMPERED' WHERE tenant_id = :t AND seq = 4"),
            {"t": tenant},
        )

    whole = await _verify(client, tenant, limit=100)
    assert whole["valid"] is False
    assert whole["first_broken"]["reason"] == "event_hash_mismatch"
    assert whole["first_broken"]["seq"] == 4

    # A window that STRADDLES the tampered row, entered as a caller reaches it.
    straddling = await _verify(client, tenant, limit=2, start_seq=3)
    assert straddling["valid"] is False, "a resumed window missed a rewritten row"
    assert straddling["first_broken"]["seq"] == 4
    assert straddling["first_broken"]["reason"] == whole["first_broken"]["reason"]
    # Window-relative, not since-genesis: seq 3 cleared before the break.
    assert straddling["verified_count"] == 1, (
        "verified_count must count rows verified in THIS window — a since-genesis "
        f"count would report 2 here: {straddling}"
    )

    # A window that STARTS on it: nothing clears, and the seed is the honest
    # row 3, so the break must still surface rather than being absorbed.
    starting = await _verify(client, tenant, limit=10, start_seq=4)
    assert starting["valid"] is False
    assert starting["first_broken"]["seq"] == 4
    assert starting["verified_count"] == 0


# ---------------------------------------------------------------------------
# L-31 — the count and the list must answer the same question.
# ---------------------------------------------------------------------------


async def test_entity_memory_count_excludes_soft_deleted_memories(client):
    """``POST /entities/count-memories`` and ``GET /entities/{id}/with-memories``
    describe the same set, and disagreed about it.

    ``entity_get_linked_memories`` filters ``Memory.deleted_at IS NULL`` and
    says so in its own docstring; the count did not, so a UI rendering the two
    together showed "3 memories" next to a list of 2. The gap is exactly the
    memories the caller deleted, which is the least explicable version of it.
    """
    from tests.test_integration import _memory_payload

    tenant = f"test-tenant-{uuid.uuid4().hex[:8]}"
    fleet = f"test-fleet-{uuid.uuid4().hex[:8]}"

    ent = await client.post(
        f"{_P}/entities",
        json={
            "tenant_id": tenant,
            "entity_type": "person",
            "canonical_name": f"Counted-{uuid.uuid4().hex[:8]}",
        },
    )
    assert ent.status_code == 200, ent.text
    entity_id = ent.json()["id"]

    memory_ids = []
    for _ in range(3):
        m = await client.post(f"{_P}/memories", json=_memory_payload(tenant, fleet))
        assert m.status_code == 200, m.text
        memory_ids.append(m.json()["id"])

    lk = await client.post(
        f"{_P}/entities/links/bulk",
        json={
            "tenant_id": tenant,
            "items": [
                {"input_idx": i, "memory_id": mid, "entity_id": entity_id, "role": "subject"}
                for i, mid in enumerate(memory_ids)
            ],
        },
    )
    assert lk.status_code == 200, lk.text

    async def _count() -> int:
        r = await client.post(
            f"{_P}/entities/count-memories",
            json={"tenant_id": tenant, "entity_ids": [entity_id]},
        )
        assert r.status_code == 200, r.text
        return r.json().get(entity_id, 0)

    async def _listed() -> int:
        r = await client.get(f"{_P}/entities/{entity_id}/with-memories", params={"tenant_id": tenant})
        assert r.status_code == 200, r.text
        return len(r.json()["linked_memories"])

    assert await _count() == 3, "setup did not link three live memories"
    assert await _listed() == 3

    d = await client.delete(f"{_P}/memories/{memory_ids[0]}", params={"tenant_id": tenant})
    assert d.status_code in (200, 204), d.text

    listed_after = await _listed()
    count_after = await _count()
    assert listed_after == 2, "the linked-memories view should drop the deleted one"
    assert count_after == listed_after, (
        f"count says {count_after} while the list shows {listed_after} — the two "
        "endpoints disagree about the same entity"
    )
