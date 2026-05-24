"""A4 #11 — ``find_entity_overlap_candidates`` optionally returns
``conflicted`` memories whose ``supersedes_id`` points at the query
target.

Context
───────
Path C (entity-overlap contradiction) calls
``find_entity_overlap_candidates(memory_id=A, ...)`` to look up
memories that share entities with the new memory A and may conflict.
Today the SQL filters
``status IN ('active','confirmed','pending')`` — so any memory B
that Path A already marked ``conflicted`` (with ``supersedes_id=A``)
is invisible. That makes retraction structurally impossible:
Path C can't retract a verdict it can't see.

A4 #11 adds an opt-in parameter ``include_supersedes: bool`` that
relaxes the status filter to ALSO return ``conflicted`` rows when
their ``supersedes_id`` equals the query target. All other filtering
(tenant, fleet, visibility, deleted_at, id) is unchanged. The
default behaviour (``include_supersedes=False``) is identical to
pre-PR.

Tests pinned BEFORE the implementation. They FAIL against current
main — the SQL has no awareness of ``supersedes_id`` in the status
filter — and PASS after the patch lands.
"""

from __future__ import annotations

import pytest

from tests.conftest import AGENT_ID, FLEET_ID, TENANT_ID, uid as _uid


pytestmark = pytest.mark.asyncio


async def _make_overlapping_memory(
    sc, content_suffix: str, *, status: str = "active"
) -> dict:
    """Create a memory under the shared tenant/fleet/agent."""
    return await sc.create_memory(
        {
            "tenant_id": TENANT_ID,
            "fleet_id": FLEET_ID,
            "agent_id": AGENT_ID,
            "content": f"A4 #11 overlap test {content_suffix} [{_uid()}]",
            "memory_type": "fact",
            "status": status,
        }
    )


async def _link_memory_to_entity(
    sc, memory_id: str, entity_id: str, role: str = "subject"
) -> None:
    """Wire memory→entity in memory_entity_links so the SQL JOIN finds overlap."""
    await sc.create_entity_link(
        {
            "memory_id": memory_id,
            "entity_id": entity_id,
            "role": role,
        }
    )


async def _make_shared_entity(sc) -> dict:
    """Create one canonical entity that all test memories will link to."""
    return await sc.create_entity(
        {
            "tenant_id": TENANT_ID,
            "fleet_id": FLEET_ID,
            "entity_type": "person",
            "canonical_name": f"a4-11 overlap {_uid()}",
        }
    )


# ---------------------------------------------------------------------------
# Chain-shape helper — mirror what Path A actually writes
# ---------------------------------------------------------------------------
#
# Path A's chain edge is **newer → older**: the active/newer row carries
# ``supersedes_id`` pointing at the conflicted/older row; the conflicted
# row itself has ``supersedes_id=NULL``. See
# ``flow-debug-contradiction-chain-shape`` memory + every write site in
# ``contradiction_detector.py``. This helper builds the same state via
# the storage client so the integration tests exercise the exact shape
# the filter must match in production.


async def _wire_path_a_retraction(sc, *, newer_id: str, older_id: str) -> None:
    """Mark ``older_id`` conflicted (no supersedes_id) and set
    ``newer_id``'s ``supersedes_id`` to point at it. This is the
    bidirectional state Path A produces: older.status=conflicted,
    older.supersedes_id=NULL, newer.supersedes_id=older."""
    await sc.update_memory_status(older_id, "conflicted")
    await sc.update_memory_status(newer_id, "active", supersedes_id=older_id)


# ---------------------------------------------------------------------------
# Default behaviour — conflicted-by-A row is INVISIBLE (current contract)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_default_excludes_conflicted_rows_unchanged_from_main(sc):
    """Default ``include_supersedes=False`` must NOT change behaviour:
    conflicted rows are excluded as today. Pins the back-compat
    contract so Phase 2 callers (Path C, others) keep their current
    semantics until they opt into the new flag."""
    a = await _make_overlapping_memory(sc, "A active subject")
    b = await _make_overlapping_memory(sc, "B about to be conflicted by A")
    active_control = await _make_overlapping_memory(sc, "C active control")

    entity = await _make_shared_entity(sc)
    for m in (a, b, active_control):
        await _link_memory_to_entity(sc, m["id"], entity["id"])

    # Reproduce Path A's actual chain shape: B conflicted (no
    # supersedes_id on B), A.supersedes_id = B.
    await _wire_path_a_retraction(sc, newer_id=a["id"], older_id=b["id"])

    # DEFAULT call — back-compat path.
    candidates = await sc.find_entity_overlap_candidates(
        {
            "memory_id": a["id"],
            "tenant_id": TENANT_ID,
            "fleet_id": FLEET_ID,
        }
    )
    ids = {c["id"] for c in candidates}

    assert active_control["id"] in ids, (
        "active control must be returned (shares the entity)"
    )
    assert b["id"] not in ids, (
        "default contract: conflicted rows are excluded from overlap candidates. "
        "If this assertion fails, the back-compat default has been broken."
    )


# ---------------------------------------------------------------------------
# Opt-in behaviour — the row Path A retracted for this memory IS returned
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_include_supersedes_returns_conflicted_supersedes_of_target(sc):
    """``include_supersedes=True`` must surface the ``conflicted`` row
    that the query memory's chain points at. This is the gate Path C
    needs to re-judge Path A's verdict (A4 #13)."""
    a = await _make_overlapping_memory(sc, "A new memory")
    b = await _make_overlapping_memory(sc, "B was conflicted by A")
    active_control = await _make_overlapping_memory(sc, "C active control")

    entity = await _make_shared_entity(sc)
    for m in (a, b, active_control):
        await _link_memory_to_entity(sc, m["id"], entity["id"])

    await _wire_path_a_retraction(sc, newer_id=a["id"], older_id=b["id"])

    candidates = await sc.find_entity_overlap_candidates(
        {
            "memory_id": a["id"],
            "tenant_id": TENANT_ID,
            "fleet_id": FLEET_ID,
            "include_supersedes": True,
        }
    )
    ids = {c["id"] for c in candidates}

    assert active_control["id"] in ids, "active control still returned alongside"
    assert b["id"] in ids, (
        f"with include_supersedes=True the row Path A retracted for A "
        f"(i.e. ``B`` such that ``A.supersedes_id == B.id``) must appear "
        f"so Path C can call the retraction primitive. Got: {ids}"
    )


@pytest.mark.integration
async def test_include_supersedes_excludes_conflicted_pointing_elsewhere(sc):
    """The opt-in is targeted, not blanket. A conflicted row that the
    QUERY MEMORY's chain does NOT point at must stay excluded.

    Setup: some other memory ``other_a`` retracted ``b_other`` (so
    ``other_a.supersedes_id = b_other``). The query memory ``a`` has
    no chain edge to ``b_other``. Even with ``include_supersedes=True``,
    ``b_other`` must NOT be returned — otherwise Path C would attempt
    retraction on conflicted rows from unrelated chains.
    """
    a = await _make_overlapping_memory(sc, "A the query target")
    other_a = await _make_overlapping_memory(sc, "other A")
    b_other = await _make_overlapping_memory(sc, "B conflicted by other_a, not A")

    entity = await _make_shared_entity(sc)
    for m in (a, other_a, b_other):
        await _link_memory_to_entity(sc, m["id"], entity["id"])

    # ``b_other`` is conflicted; ``other_a`` (NOT ``a``) carries the
    # chain edge. ``a.supersedes_id`` is NULL.
    await _wire_path_a_retraction(sc, newer_id=other_a["id"], older_id=b_other["id"])

    candidates = await sc.find_entity_overlap_candidates(
        {
            "memory_id": a["id"],
            "tenant_id": TENANT_ID,
            "fleet_id": FLEET_ID,
            "include_supersedes": True,
        }
    )
    ids = {c["id"] for c in candidates}

    assert b_other["id"] not in ids, (
        "include_supersedes is targeted by the QUERY memory's chain edge: "
        "only the conflicted row that ``memory_id.supersedes_id`` points at "
        "is returned. A conflicted row from someone else's chain must stay "
        "excluded."
    )
