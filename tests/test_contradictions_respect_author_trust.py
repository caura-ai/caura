"""A newer memory from a less-trusted author cannot overturn an older one.

Contradiction detection demoted whichever row of a confirmed pair was older and
pointed the newer row's ``supersedes_id`` at it, whoever wrote either. The
lowest-trust writer in a fleet could so retire a more-trusted agent's fact and
have its own value read as current. Now, when the newer row's author is trusted
less than the older row's, every detection path leaves both rows as they are and
records the pair as a dispute for review, whatever
``contradiction_write_conflict_record`` says. Each record names both authors'
trust and whether the verdict was applied, and the paths that undo a verdict (a
dismissal, a loser's revert, Path C's retraction of unlinked verdicts) leave a
dispute alone: it changed nothing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from core_api.constants import VECTOR_DIM
from core_api.services.contradiction_detector import (
    _detect,
    _retract_unlinked_verdicts,
    detect_contradictions_by_entities_async,
    revert_unheld_loser,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

SUBJ = "00000000-0000-0000-0000-0000000000bb"
_CD = "core_api.services.contradiction_detector"

# Authors' trust levels, as storage's agents rows hold them.
_TRUST = {"lead": 2, "member": 1}

# A confirmed contradiction in the raw shape ``_judge_contradiction`` parses.
_CONFLICT_RAW = {
    "subject_a": "X",
    "subject_b": "X",
    "same_subject": True,
    "non_conflict_reason": "none",
    "contradicts": True,
    "reason": "mutually exclusive states",
}

# The detection paths. ``path_c_rdf`` is the deterministic pass Path C re-runs
# once extraction has filled the triple.
PATHS = ("rdf", "semantic", "path_c_rdf", "path_c")


async def _get_agent(agent_id, tenant_id, read=True):
    level = _TRUST.get(agent_id)
    return None if level is None else {"agent_id": agent_id, "trust_level": level}


def _row(path: str, hour: int, value: str, author: str, **extra) -> dict:
    """A memory row. Only the RDF paths get a triple, so the others are not
    short-circuited by the deterministic pass."""
    row = {
        "id": str(uuid4()),
        "tenant_id": "t1",
        "fleet_id": "f1",
        "agent_id": author,
        "content": f"X lives in {value}",
        "status": "active",
        "visibility": "scope_team",
        "supersedes_id": None,
        "deleted_at": None,
        "created_at": f"2026-05-24T{hour:02d}:00:00+00:00",
        "subject_entity_id": None,
        "predicate": None,
        "object_value": None,
        **extra,
    }
    if path in ("rdf", "path_c_rdf"):
        row.update(subject_entity_id=SUBJ, predicate="lives_in", object_value=value)
    return row


def _storage(rows: list[dict]) -> AsyncMock:
    sc = AsyncMock()
    by_id = {r["id"]: r for r in rows}
    sc.get_memory = AsyncMock(side_effect=lambda mid, _tenant, **_kw: by_id.get(mid))
    sc.get_agent = AsyncMock(side_effect=_get_agent)
    sc.record_memory_conflict = AsyncMock(side_effect=lambda p: {**p, "id": "rec"})
    sc.batch_update_status = AsyncMock(
        return_value={"ok": True, "skipped": [], "edge_skipped": []}
    )
    return sc


async def _run(path: str, new: dict, found: list[dict], *, flag: bool = False):
    """Run one detection path with ``found`` as what its storage query returns."""
    sc = _storage([new, *found])
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
    with (
        patch(f"{_CD}.get_storage_client", return_value=sc),
        patch(
            "core_api.services.contradiction.resolver.get_storage_client",
            return_value=sc,
        ),
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
        patch("core_api.config.settings.contradiction_write_conflict_record", flag),
    ):
        if path in ("rdf", "semantic"):
            await _detect(new, [0.1] * VECTOR_DIM)
        else:
            await detect_contradictions_by_entities_async(new["id"], "t1", "f1")
    return sc


def _records(sc: AsyncMock) -> list[dict]:
    return [call.args[0] for call in sc.record_memory_conflict.await_args_list]


@pytest.mark.parametrize("path", PATHS)
async def test_a_less_trusted_newer_row_leaves_the_older_one_and_records_a_dispute(
    path,
):
    new = _row(path, 12, "Haifa", "member")
    old = _row(path, 8, "Tel Aviv", "lead")

    sc = await _run(path, new, [old])

    sc.batch_update_status.assert_not_awaited()
    (record,) = _records(sc)
    assert (record["new_memory_id"], record["old_memory_id"]) == (new["id"], old["id"])
    assert record["action"] == "mark_disputed"
    assert record["metadata"] == {
        "author_trust": {"new_memory": 1, "old_memory": 2},
        "effect_applied": False,
    }
    # The deterministic pass settled the pair: no second judgement of it.
    if path == "rdf":
        sc.find_similar_candidates.assert_not_awaited()
    if path == "path_c_rdf":
        sc.find_entity_overlap_candidates.assert_not_awaited()


@pytest.mark.parametrize("path", PATHS)
async def test_a_less_trusted_newer_candidate_leaves_the_new_row(path):
    """The flipped direction: the row just written is the older one."""
    new = _row(path, 8, "Haifa", "lead")
    newer = _row(path, 12, "Tel Aviv", "member")

    sc = await _run(path, new, [newer])

    sc.batch_update_status.assert_not_awaited()
    (record,) = _records(sc)
    assert record["action"] == "mark_disputed"
    assert record["metadata"] == {
        "author_trust": {"new_memory": 2, "old_memory": 1},
        "effect_applied": False,
    }


@pytest.mark.parametrize("path", PATHS)
async def test_a_more_trusted_newer_row_still_supersedes_and_records_both_trusts(
    path,
):
    new = _row(path, 12, "Haifa", "lead")
    old = _row(path, 8, "Tel Aviv", "member")

    sc = await _run(path, new, [old], flag=True)

    sc.batch_update_status.assert_awaited_once()
    updates = {
        u["memory_id"]: u for u in sc.batch_update_status.await_args.args[0]["updates"]
    }
    assert updates[new["id"]]["supersedes_id"] == old["id"]
    assert updates[old["id"]]["status"] in ("outdated", "conflicted")
    (record,) = _records(sc)
    assert record["action"] != "mark_disputed"
    assert record["metadata"] == {
        "author_trust": {"new_memory": 2, "old_memory": 1},
        "effect_applied": True,
    }


def _dispute(newer: dict, older: dict) -> dict:
    return {
        "id": "dispute-1",
        "tenant_id": "t1",
        "new_memory_id": newer["id"],
        "old_memory_id": older["id"],
        "review_status": "pending",
        "metadata_": {
            "author_trust": {"new_memory": 1, "old_memory": 2},
            "effect_applied": False,
        },
    }


async def test_a_dispute_does_not_keep_a_row_demoted():
    """A row another verdict demoted is reverted when that verdict is undone,
    even though a dispute names it as the older side of a live pair."""
    older = _row("semantic", 8, "Tel Aviv", "lead", status="conflicted")
    newer = _row("semantic", 12, "Haifa", "member")
    sc = _storage([older, newer])
    sc.find_by_supersedes_id = AsyncMock(return_value=[])
    sc.list_memory_conflicts = AsyncMock(return_value=[_dispute(newer, older)])

    reverted = await revert_unheld_loser(sc, "t1", older, ignore=lambda _r: False)

    assert reverted is True
    sc.update_memory_status.assert_awaited_once_with(
        older["id"], "active", tenant_id="t1"
    )


async def test_dismissing_a_dispute_changes_no_memory():
    from core_api.routes.conflicts import _undo_dismissed_verdict

    older = _row("semantic", 8, "Tel Aviv", "lead", status="conflicted")
    newer = _row("semantic", 12, "Haifa", "member")
    sc = _storage([older, newer])
    sc.find_by_supersedes_id = AsyncMock(return_value=[])
    sc.list_memory_conflicts = AsyncMock(return_value=[])

    assert await _undo_dismissed_verdict(sc, _dispute(newer, older)) == "not_applied"
    sc.update_memory_status.assert_not_awaited()


async def test_path_c_does_not_retract_through_a_dispute():
    older = _row("semantic", 8, "Tel Aviv", "lead", status="conflicted")
    newer = _row("semantic", 12, "Haifa", "member")
    sc = _storage([older, newer])
    sc.find_by_supersedes_id = AsyncMock(return_value=[])
    sc.list_memory_conflicts = AsyncMock(return_value=[_dispute(newer, older)])
    judge = AsyncMock(return_value=0.95)

    with patch(f"{_CD}._retraction_judge_clears", judge):
        assert await _retract_unlinked_verdicts(sc, newer, None) == 0

    judge.assert_not_awaited()
    sc.update_memory_status.assert_not_awaited()
