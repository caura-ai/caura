"""L-12 — Path C's forward detection must not orphan a conflicted row.

Path A (both the RDF and the semantic loop in ``_detect``) splits two
different guards:

  * ``new_memory_is_outdated`` — the status-reversion guard. Stops a
    later canonical iteration from writing ``new_memory`` back to
    ``"active"`` after an earlier flipped iteration retired it.
  * ``supersedes_id`` — the chain-edge guard. Gates ONLY the canonical
    branch (``new_memory`` can carry one outgoing edge), and leaves the
    flipped branch free to wire an edge per candidate.

``test_mixed_conflicts_complete_three_way_chain`` in
``test_contradiction_direction_invariance.py`` pins that split for Path
A, and the code comment there names the failure it prevents: "otherwise
the older canonical candidate is left orphaned (outdated but
unreachable via the chain)".

Path C's entity-overlap loop collapsed both guards into ONE ``found``
flag covering both branches, so the first confirmed conflict in a run
suppressed every later edge write while the ``"conflicted"`` status
write at the top of the loop still landed. Both mixed-direction
orderings produced the orphan Path A guards against:

  flipped → canonical: ``new_memory`` never gets its outgoing edge, so
    the older canonical candidate is ``conflicted`` with nothing
    pointing at it.
  canonical → flipped: the newer candidate never gets its edge, so
    ``new_memory`` is ``conflicted`` with nothing pointing at it.

These tests assert on the resulting chain (status + supersedes_id per
row), read straight off the ``batch_update_status`` payload.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# A confirmed contradiction, in the raw shape ``_judge_contradiction``
# parses (both safety gates aligned → verdict True at _CONF_CLEAN).
_CONFLICT_RAW = {
    "subject_a": "X",
    "subject_b": "X",
    "same_subject": True,
    "non_conflict_reason": "none",
    "contradicts": True,
    "reason": "mutually exclusive states",
}


def _row(mid, *, ts: str, content: str, status: str = "active") -> dict:
    """A memory row shaped like what storage hands Path C.

    ``subject_entity_id`` / ``predicate`` / ``object_value`` stay NULL so
    the deterministic RDF pass Path C runs first does not fire and
    short-circuit the entity-overlap loop under test.
    """
    return {
        "id": str(mid),
        "tenant_id": "t1",
        "fleet_id": "f1",
        "content": content,
        "subject_entity_id": None,
        "status": status,
        "visibility": "scope_team",
        "supersedes_id": None,
        "deleted_at": None,
        "created_at": ts,
    }


def _sc(new_mem: dict, candidates: list[dict]) -> AsyncMock:
    sc = AsyncMock()

    async def get_memory(mid: str, tenant_id: str, **_kw):
        for row in (new_mem, *candidates):
            if row["id"] == mid:
                return row
        return None

    sc.get_memory = AsyncMock(side_effect=get_memory)
    sc.find_entity_overlap_candidates = AsyncMock(return_value=candidates)
    # No entity links → empty contexts on every side → the loop selects
    # the base judge for all candidates (batched, since len > 1).
    sc.get_entity_links_for_memories = AsyncMock(return_value={})
    sc.batch_update_status = AsyncMock(return_value={"ok": True, "skipped": []})
    return sc


def _chain(sc: AsyncMock) -> dict[str, dict]:
    """The resulting chain: ``{memory_id: {status, supersedes_id}}``.

    Reads the single ``batch_update_status`` payload the loop flushes —
    rows are already merged per memory_id, so this is the state the DB
    ends up in.
    """
    assert sc.batch_update_status.await_count == 1, (
        f"expected exactly one status flush, got {sc.batch_update_status.await_count}"
    )
    payload = sc.batch_update_status.call_args.args[0]
    return {row["memory_id"]: row for row in payload["updates"]}


async def _run_path_c(new_mem: dict, candidates: list[dict]) -> AsyncMock:
    from core_api.services.contradiction_detector import (
        detect_contradictions_by_entities_async,
    )

    sc = _sc(new_mem, candidates)
    batch_judge = AsyncMock(
        side_effect=lambda _c, cands, _cfg: [_CONFLICT_RAW] * len(cands)
    )
    # A lone candidate keeps the direct per-candidate call, which returns
    # the already-judged ``(verdict, confidence)`` pair rather than a raw.
    pairwise_judge = AsyncMock(return_value=(True, 0.95))

    with (
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_contradiction_check_batch",
            batch_judge,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_contradiction_check",
            pairwise_judge,
        ),
        patch(
            "core_api.services.contradiction_detector.resolve_config",
            new_callable=AsyncMock,
            return_value=None,
            create=True,
        ),
        patch(
            "core_api.services.contradiction_detector._acquire_entity_lock",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        await detect_contradictions_by_entities_async(new_mem["id"], "t1", "f1")

    return sc


# ---------------------------------------------------------------------------
# Mixed-direction runs — the orphan shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flipped_then_canonical_completes_the_three_way_chain():
    """``candidates = [newer, older]`` — expected chain
    ``newer → new_memory → older``, all edges present.

    Pre-fix: the flipped iteration set ``found``, so the canonical
    iteration's ``new_memory → older`` edge was never written while
    ``older`` was still marked ``conflicted`` — orphaned.
    """
    new_id = str(uuid4())
    newer = _row(
        uuid4(), ts="2026-05-24T12:00:00+00:00", content="X lives in Newer Place"
    )
    older = _row(
        uuid4(), ts="2026-05-24T08:00:00+00:00", content="X lives in Older Place"
    )
    new_mem = _row(new_id, ts="2026-05-24T10:00:00+00:00", content="X lives in Haifa")

    sc = await _run_path_c(new_mem, [newer, older])
    chain = _chain(sc)

    # Edge 1 (flipped): newer → new_memory. Present pre-fix.
    assert chain[newer["id"]].get("supersedes_id") == new_id, (
        f"missing edge newer → new_memory; chain={chain}"
    )
    # Edge 2 (canonical): new_memory → older. THIS is the orphaning bug.
    assert chain[new_id].get("supersedes_id") == older["id"], (
        f"missing edge new_memory → older (orphan bug); chain={chain}"
    )
    # ...and it must not revert the flipped iteration's status write.
    assert chain[new_id]["status"] == "conflicted"
    assert chain[older["id"]]["status"] == "conflicted"

    # No row is left conflicted without an inbound edge.
    _assert_no_orphans(chain)


@pytest.mark.asyncio
async def test_canonical_then_flipped_completes_the_three_way_chain():
    """``candidates = [older, newer]`` — same expected chain, reversed
    candidate order.

    Pre-fix: the canonical iteration set ``found``, so the flipped
    iteration's ``newer → new_memory`` edge was never written while
    ``new_memory`` was still marked ``conflicted`` — orphaned.
    """
    new_id = str(uuid4())
    older = _row(
        uuid4(), ts="2026-05-24T08:00:00+00:00", content="X lives in Older Place"
    )
    newer = _row(
        uuid4(), ts="2026-05-24T12:00:00+00:00", content="X lives in Newer Place"
    )
    new_mem = _row(new_id, ts="2026-05-24T10:00:00+00:00", content="X lives in Haifa")

    sc = await _run_path_c(new_mem, [older, newer])
    chain = _chain(sc)

    assert chain[new_id].get("supersedes_id") == older["id"], (
        f"missing edge new_memory → older; chain={chain}"
    )
    assert chain[newer["id"]].get("supersedes_id") == new_id, (
        f"missing edge newer → new_memory (orphan bug); chain={chain}"
    )
    assert chain[new_id]["status"] == "conflicted"
    assert chain[older["id"]]["status"] == "conflicted"

    _assert_no_orphans(chain)


@pytest.mark.asyncio
async def test_two_older_candidates_keep_one_outgoing_edge():
    """``new_memory`` carries at most ONE outgoing edge, exactly as Path
    A's ``if not supersedes_id`` guard enforces — a row supersedes one
    predecessor, and storage's CAS would drop the second write anyway.

    Both older rows are still marked ``conflicted``; only the first
    (most relevant — candidates arrive ordered by shared-entity count)
    gets the edge. This pins that the fix widens the FLIPPED branch
    without turning the canonical branch into a multi-edge writer.
    """
    new_id = str(uuid4())
    old_a = _row(uuid4(), ts="2026-05-24T08:00:00+00:00", content="X lives in A")
    old_b = _row(uuid4(), ts="2026-05-24T09:00:00+00:00", content="X lives in B")
    new_mem = _row(new_id, ts="2026-05-24T10:00:00+00:00", content="X lives in Haifa")

    sc = await _run_path_c(new_mem, [old_a, old_b])
    chain = _chain(sc)

    assert chain[new_id].get("supersedes_id") == old_a["id"]
    assert chain[old_a["id"]]["status"] == "conflicted"
    assert chain[old_b["id"]]["status"] == "conflicted"
    assert "supersedes_id" not in chain[old_b["id"]]


@pytest.mark.asyncio
async def test_flipped_candidate_with_existing_edge_is_not_overwritten():
    """The widened flipped branch keeps the application-level guard: a
    candidate that already supersedes something must not have that edge
    clobbered (which would orphan whatever it pointed at).
    """
    new_id = str(uuid4())
    prior = str(uuid4())
    newer_a = _row(uuid4(), ts="2026-05-24T12:00:00+00:00", content="X lives in A")
    newer_b = _row(uuid4(), ts="2026-05-24T13:00:00+00:00", content="X lives in B")
    newer_b["supersedes_id"] = prior
    new_mem = _row(new_id, ts="2026-05-24T10:00:00+00:00", content="X lives in Haifa")

    sc = await _run_path_c(new_mem, [newer_a, newer_b])
    chain = _chain(sc)

    assert chain[newer_a["id"]].get("supersedes_id") == new_id
    # ``newer_b`` already points at ``prior`` — no write is emitted for it.
    assert (
        newer_b["id"] not in chain
        or chain[newer_b["id"]].get("supersedes_id") != new_id
    )
    assert chain[new_id]["status"] == "conflicted"


@pytest.mark.asyncio
async def test_existing_edge_on_new_memory_is_not_re_pointed():
    """The canonical guard is seeded from the row's current edge, as the
    semantic loop's is.

    ``memory_update_status`` has no ``supersedes_id IS NULL`` guard — the
    only CAS it offers is the explicit ``expected_supersedes_id`` the
    forward paths never pass — so an unseeded canonical write would
    silently re-point a row that already supersedes something and orphan
    its previous target. The older candidate is still marked
    ``conflicted``; what must not happen is the edge move.
    """
    new_id = str(uuid4())
    prior = str(uuid4())
    older = _row(
        uuid4(), ts="2026-05-24T08:00:00+00:00", content="X lives in Older Place"
    )
    new_mem = _row(new_id, ts="2026-05-24T10:00:00+00:00", content="X lives in Haifa")
    new_mem["supersedes_id"] = prior

    sc = await _run_path_c(new_mem, [older])
    chain = _chain(sc)

    assert chain[older["id"]]["status"] == "conflicted"
    assert new_id not in chain or chain[new_id].get("supersedes_id") != older["id"], (
        f"re-pointed new_memory's existing edge away from {prior}; chain={chain}"
    )


def _assert_no_orphans(chain: dict[str, dict]) -> None:
    """Every row this run marked ``conflicted`` must be reachable — some
    row's ``supersedes_id`` points at it. That is the invariant the
    ``found`` gate broke.
    """
    targets = {
        row["supersedes_id"] for row in chain.values() if row.get("supersedes_id")
    }
    orphans = [
        mid
        for mid, row in chain.items()
        if row["status"] == "conflicted" and mid not in targets
    ]
    assert not orphans, (
        f"conflicted but orphaned from the chain: {orphans}; chain={chain}"
    )
