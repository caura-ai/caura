"""Every loser a contradiction verdict demotes stays traceable (M-34).

``supersedes_id`` is one column. When a new memory contradicts two or more live
rows in one run, each detection loop demotes them all but wires the edge to the
first, so the rest end up ``outdated``/``conflicted`` with nothing pointing at
them: ``find_successors`` cannot say what replaced them and retraction cannot
reach them. The same happens to a loser whose winner already supersedes another
row. The ``memory_conflicts`` record was the only thing naming the winner, and
it sat behind ``contradiction_write_conflict_record``, off by default.

Such a loser is now recorded whatever the flag. A loser the edge points at is
not, and with the flag on every pair is still recorded once. An edge counts
only if it landed: storage's ``expect_supersedes_null`` CAS can lose to a
concurrent writer, and then the winner keeps its old pointer and the loser has
no edge at all.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from core_api.constants import VECTOR_DIM
from core_api.services.contradiction_detector import (
    _detect,
    detect_contradictions_by_entities_async,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

SUBJ = "00000000-0000-0000-0000-0000000000aa"
_CD = "core_api.services.contradiction_detector"
_RECORD = "core_api.services.contradiction.resolver.record_detected_conflicts"

# A confirmed contradiction in the raw shape ``_judge_contradiction`` parses.
_CONFLICT_RAW = {
    "subject_a": "X",
    "subject_b": "X",
    "same_subject": True,
    "non_conflict_reason": "none",
    "contradicts": True,
    "reason": "mutually exclusive states",
}

# path -> (record kind, status the loser is demoted to). ``path_c_rdf`` is the
# deterministic pass Path C re-runs once extraction has filled the triple.
PATHS = {
    "rdf": ("rdf", "outdated"),
    "semantic": ("semantic", "conflicted"),
    "path_c_rdf": ("rdf", "outdated"),
    "path_c": ("entity", "conflicted"),
}


def _row(path: str, hour: int, value: str, *, supersedes_id=None) -> dict:
    """A memory row. Only the RDF paths get a triple, so the others are not
    short-circuited by the deterministic pass."""
    row = {
        "id": str(uuid4()),
        "tenant_id": "t1",
        "fleet_id": "f1",
        "content": f"X lives in {value}",
        "status": "active",
        "visibility": "scope_team",
        "supersedes_id": supersedes_id,
        "deleted_at": None,
        "created_at": f"2026-05-24T{hour:02d}:00:00+00:00",
        "subject_entity_id": None,
        "predicate": None,
        "object_value": None,
    }
    if path in ("rdf", "path_c_rdf"):
        row.update(subject_entity_id=SUBJ, predicate="lives_in", object_value=value)
    return row


async def _run(
    path: str,
    new: dict,
    found: list[dict],
    *,
    flag: bool = False,
    flushed: dict | None = None,
):
    """Run one detection path with ``found`` as what its storage query returns
    and ``flushed`` as what its status flush answers."""
    sc = AsyncMock()
    sc.find_rdf_conflicts = AsyncMock(
        return_value=found if path in ("rdf", "path_c_rdf") else []
    )
    sc.find_similar_candidates = AsyncMock(
        return_value=found if path == "semantic" else []
    )
    sc.find_entity_overlap_candidates = AsyncMock(
        return_value=found if path == "path_c" else []
    )
    sc.get_entity_links_for_memories = AsyncMock(return_value={})
    rows = {r["id"]: r for r in (new, *found)}
    sc.get_memory = AsyncMock(side_effect=lambda mid, _tenant, **_kw: rows.get(mid))
    sc.batch_update_status = AsyncMock(
        return_value=flushed or {"ok": True, "skipped": [], "edge_skipped": []}
    )
    rec = AsyncMock()
    with (
        patch(f"{_CD}.get_storage_client", return_value=sc),
        patch(
            f"{_CD}._llm_contradiction_check_batch",
            AsyncMock(side_effect=lambda _c, cands, _cfg: [_CONFLICT_RAW] * len(cands)),
        ),
        patch(f"{_CD}._llm_contradiction_check", AsyncMock(return_value=(True, 0.95))),
        patch(f"{_CD}._acquire_entity_lock", AsyncMock(return_value=True)),
        patch(
            "core_api.services.organization_settings.resolve_config",
            AsyncMock(return_value=None),
        ),
        patch(_RECORD, rec),
        patch("core_api.config.settings.contradiction_write_conflict_record", flag),
    ):
        if path in ("rdf", "semantic"):
            await _detect(new, [0.1] * VECTOR_DIM)
        else:
            await detect_contradictions_by_entities_async(new["id"], "t1", "f1")
    return sc, rec


def _chain(sc: AsyncMock) -> dict[str, dict]:
    """``{memory_id: row}`` from the one status flush the run made."""
    assert sc.batch_update_status.await_count == 1
    return {
        u["memory_id"]: u for u in sc.batch_update_status.call_args.args[0]["updates"]
    }


def _recorded(rec: AsyncMock) -> list[tuple[str, str]]:
    """``(candidate id, kind)`` for every pair the run recorded."""
    return [
        (cand["id"], kind)
        for call in rec.await_args_list
        for cand, kind, _confidence in call.args[1]
    ]


@pytest.mark.parametrize("path", PATHS)
async def test_the_loser_no_edge_points_at_is_recorded(path):
    kind, demoted = PATHS[path]
    new = _row(path, 12, "Haifa")
    first, second = _row(path, 8, "Tel Aviv"), _row(path, 9, "Eilat")

    sc, rec = await _run(path, new, [first, second])

    chain = _chain(sc)
    assert chain[new["id"]]["supersedes_id"] == first["id"]
    assert chain[second["id"]]["status"] == demoted
    assert _recorded(rec) == [(second["id"], kind)]


@pytest.mark.parametrize("path", PATHS)
async def test_a_lone_loser_the_edge_points_at_is_not_recorded(path):
    new = _row(path, 12, "Haifa")
    old = _row(path, 8, "Tel Aviv")

    sc, rec = await _run(path, new, [old])

    assert _chain(sc)[new["id"]]["supersedes_id"] == old["id"]
    rec.assert_not_awaited()


@pytest.mark.parametrize("path", PATHS)
async def test_a_loser_whose_edge_lost_the_cas_is_recorded(path):
    kind, _demoted = PATHS[path]
    new = _row(path, 12, "Haifa")
    old = _row(path, 8, "Tel Aviv")
    lost = {"ok": True, "skipped": [], "edge_skipped": [new["id"]]}

    sc, rec = await _run(path, new, [old], flushed=lost)

    assert _chain(sc)[new["id"]]["supersedes_id"] == old["id"]
    assert _recorded(rec) == [(old["id"], kind)]


async def test_a_loser_whose_winner_already_supersedes_a_row_is_recorded():
    new = _row("semantic", 12, "Haifa", supersedes_id=str(uuid4()))
    old = _row("semantic", 8, "Tel Aviv")

    sc, rec = await _run("semantic", new, [old])

    chain = _chain(sc)
    assert chain[old["id"]]["status"] == "conflicted"
    assert "supersedes_id" not in chain.get(new["id"], {})
    assert _recorded(rec) == [(old["id"], "semantic")]


async def test_a_flipped_loser_whose_winner_keeps_its_own_edge_is_recorded():
    new = _row("semantic", 12, "Haifa")
    newer = _row("semantic", 14, "Eilat", supersedes_id=str(uuid4()))

    sc, rec = await _run("semantic", new, [newer])

    chain = _chain(sc)
    assert chain[new["id"]]["status"] == "conflicted"
    assert "supersedes_id" not in chain.get(newer["id"], {})
    assert _recorded(rec) == [(newer["id"], "semantic")]


async def test_with_the_flag_on_every_pair_is_recorded_once():
    new = _row("rdf", 12, "Haifa")
    first, second = _row("rdf", 8, "Tel Aviv"), _row("rdf", 9, "Eilat")

    _sc, rec = await _run("rdf", new, [first, second], flag=True)

    rec.assert_awaited_once()
    assert _recorded(rec) == [(first["id"], "rdf"), (second["id"], "rdf")]


async def test_with_the_flag_on_path_c_records_its_verdicts_too():
    """The resolver was written for Path C's pairs, but the entity-overlap loop
    never handed it any, so the flag recorded nothing there."""
    new = _row("path_c", 12, "Haifa")
    old = _row("path_c", 8, "Tel Aviv")

    _sc, rec = await _run("path_c", new, [old], flag=True)

    assert _recorded(rec) == [(old["id"], "entity")]
