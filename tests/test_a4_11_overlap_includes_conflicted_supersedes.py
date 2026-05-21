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

    # Mark B as conflicted by A using the A4 #10 retraction-capable
    # ``update_memory_status``.
    await sc.update_memory_status(b["id"], "conflicted", supersedes_id=a["id"])

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
# Opt-in behaviour — conflicted-by-A row IS returned when requested
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_include_supersedes_returns_conflicted_supersedes_of_target(sc):
    """``include_supersedes=True`` must surface a ``conflicted`` row whose
    ``supersedes_id`` equals the query target. This is the gate Path C
    needs to retract Path A's verdict."""
    a = await _make_overlapping_memory(sc, "A new memory")
    b = await _make_overlapping_memory(sc, "B was conflicted by A")
    active_control = await _make_overlapping_memory(sc, "C active control")

    entity = await _make_shared_entity(sc)
    for m in (a, b, active_control):
        await _link_memory_to_entity(sc, m["id"], entity["id"])

    await sc.update_memory_status(b["id"], "conflicted", supersedes_id=a["id"])

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
        f"with include_supersedes=True the conflicted-by-A row B must appear "
        f"so Path C can call the retraction primitive. Got: {ids}"
    )


@pytest.mark.integration
async def test_include_supersedes_excludes_conflicted_pointing_elsewhere(sc):
    """The opt-in is targeted, not blanket. A conflicted row whose
    ``supersedes_id`` does NOT equal the query target must STILL be
    excluded under ``include_supersedes=True``.

    Otherwise Path C would attempt retraction on conflicted rows that
    were superseded by an entirely different memory — wrong scope.
    """
    a = await _make_overlapping_memory(sc, "A the query target")
    other_a = await _make_overlapping_memory(sc, "other A")
    b_conflicted_by_other = await _make_overlapping_memory(
        sc, "B conflicted by some other memory"
    )

    entity = await _make_shared_entity(sc)
    for m in (a, other_a, b_conflicted_by_other):
        await _link_memory_to_entity(sc, m["id"], entity["id"])

    # B was conflicted by ``other_a``, NOT by ``a``.
    await sc.update_memory_status(
        b_conflicted_by_other["id"], "conflicted", supersedes_id=other_a["id"]
    )

    candidates = await sc.find_entity_overlap_candidates(
        {
            "memory_id": a["id"],
            "tenant_id": TENANT_ID,
            "fleet_id": FLEET_ID,
            "include_supersedes": True,
        }
    )
    ids = {c["id"] for c in candidates}

    assert b_conflicted_by_other["id"] not in ids, (
        "include_supersedes is targeted: only conflicted rows whose "
        "supersedes_id equals the query target are returned. A row "
        "conflicted by a different memory must stay excluded."
    )
