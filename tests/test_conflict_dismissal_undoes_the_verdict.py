"""Dismissing a conflict undoes what detection did to the pair (M-102).

A ``dismissed`` review is the reviewer saying detection was wrong. The route
recorded that and touched no memory row, and nothing else ever read the
decision: the loser stayed ``conflicted``/``outdated`` (excluded from recall
or penalised) and the winner's ``supersedes_id`` kept presenting it as
corrected. No public API clears that edge.

The dismissal now clears the winner's edge through the same compare-and-swap
retraction uses, then reverts the loser if detection's status is still on it
and no other row still points at it. A chain that has moved on since is not the
dismissal's to undo, and the decision is recorded whatever happens to the rows.
The audit entry says how far the undo got, and tells a storage failure, which
leaves the verdict in place for a person to fix, from there being nothing to undo.

A verdict detection left without a chain edge (M-34: a winner wires its one
``supersedes_id`` to its first loser only, or to none when it already supersedes
a row) is known only from its record. Dismissing one returned ``not_applied``
and left the loser demoted (M-102). The undo now reverts that loser, the older
row of the pair, unless something else still holds it: an edge, or another
standing record that it lost to a live, newer row. A loser whose winner's edge
has since moved on is the same shape and is reverted the same way.

L-228: a conflict storage does not have, or another tenant's, is a 404 before
any undo or audit, not an undo reported as failed.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from core_api.auth import AuthContext
from core_api.routes import conflicts
from core_api.schemas import ConflictResolveRequest

pytestmark = pytest.mark.asyncio


def _mem(mid: str, *, status: str, supersedes_id: str | None) -> dict:
    return {
        "id": mid,
        "tenant_id": "t1",
        "status": status,
        "supersedes_id": supersedes_id,
        "deleted_at": None,
    }


def _http_error(status: int) -> httpx.HTTPStatusError:
    """What the storage client raises for a non-404 error status."""
    request = httpx.Request("PATCH", "http://storage/memories/m/status")
    return httpx.HTTPStatusError(
        str(status), request=request, response=httpx.Response(status, request=request)
    )


def _conflict(new_id: str, old_id: str, review_status: str) -> dict:
    return {
        "id": str(uuid4()),
        "tenant_id": "t1",
        "new_memory_id": new_id,
        "old_memory_id": old_id,
        "relationship": "exact_value",
        "review_status": review_status,
    }


async def _run_review(
    review_status: str,
    new: dict,
    old: dict,
    *,
    edge_clear=None,
    revert_error=None,
    read_error=None,
    holders=(),
    status_on_reread=None,
    records=(),
    extra_rows=(),
):
    """Drive ``resolve_conflict`` with storage mocked.

    ``holders`` are the rows ``find_by_supersedes_id`` reports pointing at the
    loser; ``status_on_reread`` is the status a row reports from its second
    read on, as if another writer changed it in between. ``records`` are the
    other conflict records storage reports naming a memory, alongside the one
    under review, and ``extra_rows`` the memories they name. ``reads`` logs each
    memory read, holder lookup and record lookup with its keyword arguments.
    """
    sc = AsyncMock()
    reviewed = _conflict(new["id"], old["id"], review_status)
    sc.resolve_memory_conflict = AsyncMock(return_value=reviewed)
    rows = {new["id"]: new, old["id"]: old, **{r["id"]: r for r in extra_rows}}
    reads: list[tuple[str, dict]] = []

    def get_memory(mid, _tenant, **kw):
        if read_error is not None:
            raise read_error
        earlier = [r for r, _ in reads if r == mid]
        reads.append((mid, kw))
        row = rows.get(mid)
        if row is not None and earlier and status_on_reread is not None:
            return {**row, "status": status_on_reread}
        return row

    sc.get_memory = AsyncMock(side_effect=get_memory)

    async def find_by_supersedes_id(_tenant, _mid, **kw):
        reads.append(("holders", kw))
        return list(holders)

    sc.find_by_supersedes_id = AsyncMock(side_effect=find_by_supersedes_id)

    async def list_memory_conflicts(_tenant, **kw):
        reads.append(("records", kw))
        return [reviewed, *records]

    sc.list_memory_conflicts = AsyncMock(side_effect=list_memory_conflicts)
    writes: list[tuple] = []

    async def update_memory_status(mid, status, supersedes_id=None, **kw):
        writes.append((mid, status, kw.get("unset_supersedes", False), kw))
        if kw.get("unset_supersedes") and edge_clear is not None:
            raise edge_clear
        if not kw.get("unset_supersedes") and revert_error is not None:
            raise revert_error

    sc.update_memory_status = AsyncMock(side_effect=update_memory_status)
    log = AsyncMock()
    with (
        patch.object(conflicts, "get_storage_client", return_value=sc),
        patch.object(
            conflicts, "_require_trust", AsyncMock(return_value=(2, False, None))
        ),
        patch.object(conflicts, "log_action", log),
    ):
        out = await conflicts.resolve_conflict(
            "c1",
            ConflictResolveRequest(tenant_id="t1", review_status=review_status),
            auth=AuthContext(tenant_id="t1"),
        )
    return SimpleNamespace(
        out=out, writes=writes, reads=reads, detail=log.await_args.kwargs["detail"]
    )


async def _review(review_status: str, new: dict, old: dict, **kw):
    r = await _run_review(review_status, new, old, **kw)
    return r.out, r.writes


def _canonical():
    old_id, new_id = str(uuid4()), str(uuid4())
    return (
        _mem(new_id, status="active", supersedes_id=old_id),
        _mem(old_id, status="conflicted", supersedes_id=None),
    )


def _flipped():
    old_id, new_id = str(uuid4()), str(uuid4())
    return (
        _mem(new_id, status="outdated", supersedes_id=None),
        _mem(old_id, status="active", supersedes_id=new_id),
    )


def _unlinked(*, winner_edge: str | None = None):
    """A verdict that left no chain edge (M-34): the older row is demoted and
    nothing points at it. ``winner_edge`` is what the winner supersedes instead."""
    old_id, new_id = str(uuid4()), str(uuid4())
    new = _mem(new_id, status="active", supersedes_id=winner_edge)
    old = _mem(old_id, status="conflicted", supersedes_id=None)
    new["created_at"] = "2026-10-02T00:00:00+00:00"
    old["created_at"] = "2026-10-01T00:00:00+00:00"
    return new, old


def _rival(created_at: str = "2026-10-03T00:00:00+00:00") -> dict:
    """Another winner, with no chain edge of its own."""
    rival = _mem(str(uuid4()), status="active", supersedes_id=None)
    rival["created_at"] = created_at
    return rival


def _reverts(writes: list[tuple]) -> list[tuple]:
    return [w for w in writes if not w[2]]


async def test_dismissing_a_canonical_verdict_reverts_the_loser_and_clears_the_edge():
    new, old = _canonical()
    out, writes = await _review("dismissed", new, old)

    assert out.review_status == "dismissed"
    clear = [w for w in writes if w[2]]
    assert len(clear) == 1
    assert clear[0][0] == new["id"]
    assert clear[0][3]["expected_supersedes_id"] == old["id"]
    assert (old["id"], "active", False) in [(w[0], w[1], w[2]) for w in writes]


async def test_dismissing_a_flipped_verdict_undoes_it_the_other_way_round():
    new, old = _flipped()
    _out, writes = await _review("dismissed", new, old)

    clear = [w for w in writes if w[2]]
    assert len(clear) == 1
    assert clear[0][0] == old["id"]
    assert clear[0][3]["expected_supersedes_id"] == new["id"]
    assert (new["id"], "active", False) in [(w[0], w[1], w[2]) for w in writes]


async def test_resolving_a_conflict_touches_no_memory():
    new, old = _canonical()
    _out, writes = await _review("resolved", new, old)
    assert writes == []


async def test_a_loser_someone_else_moved_keeps_its_status():
    new, old = _canonical()
    old["status"] = "confirmed"  # a person confirmed it after detection ran
    _out, writes = await _review("dismissed", new, old)
    assert [w[0] for w in writes if w[2]] == [new["id"]]
    assert all(w[2] for w in writes), f"the loser's status was rewritten: {writes}"


async def test_a_refused_compare_and_swap_reverts_nothing_and_keeps_the_record():
    new, old = _canonical()
    # The storage CAS's answer when another writer moved the chain since.
    r = await _run_review("dismissed", new, old, edge_clear=_http_error(409))
    assert r.out.review_status == "dismissed"
    assert not _reverts(r.writes), f"the loser was reverted anyway: {r.writes}"
    assert r.detail["undo"] == "not_applied"


async def test_the_audit_entry_says_the_verdict_was_undone():
    new, old = _canonical()
    r = await _run_review("dismissed", new, old)
    assert r.detail["undo"] == "undone"


async def test_a_loser_another_row_still_points_at_keeps_its_status():
    """A second winner still marks it corrected. Reverting it would bring back a
    memory that chain still supersedes."""
    new, old = _canonical()
    other = _mem(str(uuid4()), status="active", supersedes_id=old["id"])
    r = await _run_review("dismissed", new, old, holders=[other])
    assert [w[0] for w in r.writes if w[2]] == [new["id"]]
    assert not _reverts(r.writes), f"the loser was reverted: {r.writes}"
    assert r.detail["undo"] == "undone"


async def test_the_loser_is_read_again_just_before_it_is_reverted():
    """There is no expected-status guard on the revert, so the status it checks
    must be as fresh as storage allows, not the one read before the edge write."""
    new, old = _canonical()
    r = await _run_review("dismissed", new, old, status_on_reread="confirmed")
    assert [w[0] for w in r.writes if w[2]] == [new["id"]]
    assert not _reverts(r.writes), f"a status set since was overwritten: {r.writes}"


async def test_every_read_goes_to_the_writer():
    """Each read decides a write made straight after it; a lagging replica could
    show the edge or the status before the latest change."""
    new, old = _canonical()
    r = await _run_review("dismissed", new, old)
    assert any(mid == "holders" for mid, _kw in r.reads), r.reads
    assert all(kw.get("read") is False for _mid, kw in r.reads), r.reads


async def test_a_failed_revert_after_the_edge_is_cleared_is_reported_as_partial():
    """The decision stands and cannot be filed again, so the audit entry has to
    say the loser is still demoted for a person to fix."""
    new, old = _canonical()
    r = await _run_review(
        "dismissed", new, old, revert_error=RuntimeError("storage unavailable")
    )
    assert r.out.review_status == "dismissed"
    assert [w[0] for w in r.writes if w[2]] == [new["id"]]
    assert r.detail["undo"] == "partial"


@pytest.mark.parametrize(
    "failure",
    [
        {"edge_clear": _http_error(503)},
        {"edge_clear": httpx.ReadTimeout("storage did not answer")},
        {"read_error": httpx.ConnectError("storage unreachable")},
    ],
)
async def test_a_storage_failure_before_the_edge_is_cleared_is_reported_as_failed(
    failure, caplog
):
    """Not ``not_applied``: the verdict is still fully in place and the decision
    cannot be filed again, so the entry and the log must say it needs a person."""
    new, old = _canonical()
    with caplog.at_level(logging.ERROR, logger=conflicts.__name__):
        r = await _run_review("dismissed", new, old, **failure)
    assert r.out.review_status == "dismissed"
    assert not _reverts(r.writes)
    assert r.detail["undo"] == "failed"
    assert any(rec.levelno >= logging.ERROR for rec in caplog.records), caplog.records


async def test_a_partial_undo_is_logged_as_an_error(caplog):
    new, old = _canonical()
    with caplog.at_level(logging.ERROR, logger=conflicts.__name__):
        await _run_review(
            "dismissed", new, old, revert_error=RuntimeError("storage unavailable")
        )
    assert any(rec.levelno >= logging.ERROR for rec in caplog.records), caplog.records


# ── M-102 / M-34: a verdict that left no chain edge ───────────────────────


@pytest.mark.parametrize(
    "winner_edge", [False, True], ids=["no_edge", "edge_elsewhere"]
)
async def test_dismissing_an_unlinked_verdict_reverts_its_loser(winner_edge):
    """The record is all that names this loser, and the dismissal says the
    verdict was wrong. Whether the winner never pointed at it or points
    elsewhere now, no edge is rewritten: there is none to clear."""
    new, old = _unlinked(winner_edge=str(uuid4()) if winner_edge else None)
    r = await _run_review("dismissed", new, old)
    assert not [w for w in r.writes if w[2]], f"an edge was rewritten: {r.writes}"
    assert [(w[0], w[1]) for w in _reverts(r.writes)] == [(old["id"], "active")]
    assert r.detail["undo"] == "undone"


async def test_the_older_row_is_the_loser_whichever_side_it_was_filed_on():
    """Detection picks the loser by age after the verdict, so the record's
    ``new_memory_id`` can be the row it demoted."""
    new, old = _unlinked()
    new["status"], old["status"] = "outdated", "active"
    new["created_at"], old["created_at"] = old["created_at"], new["created_at"]
    r = await _run_review("dismissed", new, old)
    assert [(w[0], w[1]) for w in _reverts(r.writes)] == [(new["id"], "active")]


async def test_an_unlinked_loser_another_standing_verdict_holds_keeps_its_status():
    """A newer, live row's record, not dismissed, says it lost there too."""
    new, old = _unlinked()
    rival = _rival()
    held = _conflict(rival["id"], old["id"], "pending")
    r = await _run_review("dismissed", new, old, records=[held], extra_rows=[rival])
    assert not _reverts(r.writes), f"the loser was reverted: {r.writes}"
    assert r.detail["undo"] == "not_applied"


@pytest.mark.parametrize("why", ["dismissed", "deleted", "it_won"])
async def test_a_record_that_no_longer_holds_the_loser_is_set_aside(why):
    """A record reviewers dismissed, one whose winner is gone, and one the
    loser itself won (it is the newer row there) demote nothing now."""
    new, old = _unlinked()
    rival = _rival(
        "2026-09-01T00:00:00+00:00" if why == "it_won" else "2026-10-03T00:00:00+00:00"
    )
    if why == "deleted":
        rival["deleted_at"] = "2026-10-04T00:00:00+00:00"
    record = _conflict(
        rival["id"], old["id"], "dismissed" if why == "dismissed" else "pending"
    )
    r = await _run_review("dismissed", new, old, records=[record], extra_rows=[rival])
    assert [(w[0], w[1]) for w in _reverts(r.writes)] == [(old["id"], "active")]


async def test_an_unlinked_loser_an_edge_still_holds_keeps_its_status():
    new, old = _unlinked()
    other = _mem(str(uuid4()), status="active", supersedes_id=old["id"])
    r = await _run_review("dismissed", new, old, holders=[other])
    assert not _reverts(r.writes), f"the loser was reverted: {r.writes}"
    assert r.detail["undo"] == "not_applied"


async def test_an_unlinked_loser_someone_else_moved_keeps_its_status():
    new, old = _unlinked()
    old["status"] = "confirmed"
    r = await _run_review("dismissed", new, old)
    assert r.writes == []
    assert r.detail["undo"] == "not_applied"


async def test_the_unlinked_undo_reads_from_the_writer():
    new, old = _unlinked()
    rival = _rival()
    record = _conflict(rival["id"], old["id"], "pending")
    rival["deleted_at"] = "2026-10-04T00:00:00+00:00"
    r = await _run_review("dismissed", new, old, records=[record], extra_rows=[rival])
    assert {"records", "holders", rival["id"]} <= {mid for mid, _kw in r.reads}, r.reads
    assert all(kw.get("read") is False for _mid, kw in r.reads), r.reads


async def test_a_failed_unlinked_revert_is_reported_as_failed():
    """Nothing was written, so the verdict is fully in place: not ``partial``."""
    new, old = _unlinked()
    r = await _run_review(
        "dismissed", new, old, revert_error=RuntimeError("storage unavailable")
    )
    assert r.out.review_status == "dismissed"
    assert r.detail["undo"] == "failed"


# ── L-228: an unknown or foreign conflict ─────────────────────────────────


@pytest.mark.parametrize("review_status", ["dismissed", "resolved"])
async def test_an_unknown_or_foreign_conflict_is_a_404_with_no_undo_or_audit(
    review_status,
):
    sc = AsyncMock()
    sc.resolve_memory_conflict = AsyncMock(return_value=None)
    log = AsyncMock()
    with (
        patch.object(conflicts, "get_storage_client", return_value=sc),
        patch.object(
            conflicts, "_require_trust", AsyncMock(return_value=(2, False, None))
        ),
        patch.object(conflicts, "log_action", log),
        pytest.raises(HTTPException) as raised,
    ):
        await conflicts.resolve_conflict(
            "c1",
            ConflictResolveRequest(tenant_id="t1", review_status=review_status),
            auth=AuthContext(tenant_id="t1"),
        )
    assert raised.value.status_code == 404
    log.assert_not_awaited()
    sc.get_memory.assert_not_awaited()
