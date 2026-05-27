"""Audit P2 — collapse sequential ``update_memory_status`` calls in the
contradiction detector into one ``batch_update_status`` per detection path.

These shape-explicit tests assert the post-P2 wire effect directly:
- Exactly one ``sc.batch_update_status({"updates": [...]})`` call per path
- Zero per-row ``sc.update_memory_status`` calls (the legacy shape)
- All affected memory_ids present in the batch payload, with the same
  status / supersedes_id values the serial path produced

Companion to the legacy-shape assertions in
``test_contradiction_direction_invariance.py`` and ``test_p1_contradiction.py``,
which now route through ``install_batch_status_replay_shim`` and stay
backward-compatible.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from core_api.constants import VECTOR_DIM
from core_api.services.contradiction_detector import _detect

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _make_new(
    *, mid: str, ts: str = "2026-04-29T12:00:00+00:00", object_value: str = "B"
) -> dict:
    return {
        "id": mid,
        "tenant_id": "t1",
        "fleet_id": "f1",
        "content": f"X lives in {object_value}",
        "subject_entity_id": "00000000-0000-0000-0000-0000000000aa",
        "predicate": "lives_in",
        "object_value": object_value,
        "deleted_at": None,
        "status": "active",
        "visibility": "scope_team",
        "supersedes_id": None,
        "created_at": ts,
    }


def _make_cand(
    *, cid: str, ts: str = "2026-04-29T10:00:00+00:00", object_value: str = "A"
) -> dict:
    return {
        "id": cid,
        "content": f"X lives in {object_value}",
        "status": "active",
        "object_value": object_value,
        "created_at": ts,
    }


# ----------------------------------------------------------------------------
# RDF path — one batch call covering all rdf_conflicts
# ----------------------------------------------------------------------------


async def test_rdf_path_collapses_to_one_batch_call():
    """Three RDF conflicts → one ``batch_update_status`` HTTP, not three
    (audit P2 RDF path: lines 300/322/347 pre-fix)."""
    new_id = str(uuid4())
    new = _make_new(mid=new_id)

    # Three older candidates — canonical direction for each.
    cands = [_make_cand(cid=str(uuid4()), object_value=f"v{i}") for i in range(3)]

    mock_sc = AsyncMock()
    mock_sc.find_rdf_conflicts = AsyncMock(return_value=cands)
    mock_sc.batch_update_status = AsyncMock(return_value={"ok": True, "skipped": []})
    mock_sc.update_memory_status = AsyncMock()
    mock_sc.find_similar_candidates = AsyncMock(return_value=[])

    with patch(
        "core_api.services.contradiction_detector.get_storage_client",
        return_value=mock_sc,
    ):
        await _detect(new, [0.1] * VECTOR_DIM)

    assert mock_sc.update_memory_status.call_count == 0, (
        f"per-row update_memory_status must be 0 after P2; got "
        f"{mock_sc.update_memory_status.call_count} call(s)"
    )
    assert mock_sc.batch_update_status.call_count == 1, (
        f"expected exactly 1 batch_update_status; got "
        f"{mock_sc.batch_update_status.call_count}"
    )
    payload = mock_sc.batch_update_status.call_args.args[0]
    # Per candidate the canonical path writes 1 row (older → outdated) plus,
    # for the FIRST canonical candidate only, 1 more row (new_memory →
    # supersedes_id wired). With 3 same-direction candidates that's 3 + 1 = 4.
    assert len(payload["updates"]) == 4

    older_rows = [u for u in payload["updates"] if u["status"] == "outdated"]
    new_rows = [u for u in payload["updates"] if u["memory_id"] == new_id]
    assert len(older_rows) == 3
    assert len(new_rows) == 1
    assert "supersedes_id" in new_rows[0]


# ----------------------------------------------------------------------------
# Semantic path — one batch call covering all gather'd verdicts
# ----------------------------------------------------------------------------


async def test_semantic_path_collapses_to_one_batch_call():
    """Two semantic-conflict verdicts → one batch call (lines 433/446/463
    pre-fix)."""
    new_id = str(uuid4())
    # Use a non-RDF-eligible predicate so we go straight to the semantic
    # path. Easiest: drop subject_entity_id/predicate/object_value.
    new = _make_new(mid=new_id)
    new["subject_entity_id"] = None
    new["predicate"] = None
    new["object_value"] = None

    cands = [_make_cand(cid=str(uuid4()), object_value=f"v{i}") for i in range(2)]

    mock_sc = AsyncMock()
    mock_sc.find_rdf_conflicts = AsyncMock(return_value=[])  # not reached
    mock_sc.find_similar_candidates = AsyncMock(return_value=cands)
    mock_sc.batch_update_status = AsyncMock(return_value={"ok": True, "skipped": []})
    mock_sc.update_memory_status = AsyncMock()

    with (
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=mock_sc,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_contradiction_check",
            AsyncMock(return_value=(True, 0.9)),  # both verdicts: contradict
        ),
    ):
        await _detect(new, [0.1] * VECTOR_DIM)

    assert mock_sc.update_memory_status.call_count == 0
    assert mock_sc.batch_update_status.call_count == 1
    payload = mock_sc.batch_update_status.call_args.args[0]
    # Two canonical candidates ⇒ 2 older-conflicted rows + 1 new_memory
    # row (only the FIRST canonical candidate wires supersedes_id; the
    # ``if not supersedes_id`` gate skips the rest). Total 3.
    assert len(payload["updates"]) == 3

    conflicted = [
        u
        for u in payload["updates"]
        if u["status"] == "conflicted" and u["memory_id"] != new_id
    ]
    new_row = [u for u in payload["updates"] if u["memory_id"] == new_id]
    assert len(conflicted) == 2
    assert len(new_row) == 1
    assert new_row[0].get("supersedes_id") is not None


# ----------------------------------------------------------------------------
# Empty paths — no batch call when there are no conflicts
# ----------------------------------------------------------------------------


async def test_rdf_path_empty_conflicts_skips_batch_call():
    """No RDF conflicts → no batch_update_status HTTP (avoid empty round-trip)."""
    new = _make_new(mid=str(uuid4()))

    mock_sc = AsyncMock()
    mock_sc.find_rdf_conflicts = AsyncMock(return_value=[])
    mock_sc.find_similar_candidates = AsyncMock(return_value=[])
    mock_sc.batch_update_status = AsyncMock()
    mock_sc.update_memory_status = AsyncMock()

    with patch(
        "core_api.services.contradiction_detector.get_storage_client",
        return_value=mock_sc,
    ):
        await _detect(new, [0.1] * VECTOR_DIM)

    assert mock_sc.batch_update_status.call_count == 0
    assert mock_sc.update_memory_status.call_count == 0


async def test_semantic_path_no_verdicts_skips_batch_call():
    """All candidates non-contradictory → no batch call."""
    new = _make_new(mid=str(uuid4()))
    new["subject_entity_id"] = None
    new["predicate"] = None
    new["object_value"] = None

    cands = [_make_cand(cid=str(uuid4()))]

    mock_sc = AsyncMock()
    mock_sc.find_rdf_conflicts = AsyncMock(return_value=[])
    mock_sc.find_similar_candidates = AsyncMock(return_value=cands)
    mock_sc.batch_update_status = AsyncMock()
    mock_sc.update_memory_status = AsyncMock()

    with (
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=mock_sc,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_contradiction_check",
            AsyncMock(return_value=(False, 0.1)),  # no verdict
        ),
    ):
        await _detect(new, [0.1] * VECTOR_DIM)

    assert mock_sc.batch_update_status.call_count == 0
    assert mock_sc.update_memory_status.call_count == 0


# ----------------------------------------------------------------------------
# Mixed gather results — exception in one candidate doesn't break the batch
# ----------------------------------------------------------------------------


async def test_semantic_path_skips_exception_candidate_still_batches():
    """One LLM call raises, one returns verdict=True. The exception
    candidate is skipped; the successful one still ends up in the batch."""
    new_id = str(uuid4())
    new = _make_new(mid=new_id)
    new["subject_entity_id"] = None
    new["predicate"] = None
    new["object_value"] = None

    cands = [
        _make_cand(cid=str(uuid4()), object_value="explode"),
        _make_cand(cid=str(uuid4()), object_value="ok"),
    ]

    async def _judge(new_content, old_content, _cfg):
        if "explode" in old_content:
            raise TimeoutError("simulated llm timeout")
        return (True, 0.9)

    mock_sc = AsyncMock()
    mock_sc.find_rdf_conflicts = AsyncMock(return_value=[])
    mock_sc.find_similar_candidates = AsyncMock(return_value=cands)
    mock_sc.batch_update_status = AsyncMock(return_value={"ok": True, "skipped": []})
    mock_sc.update_memory_status = AsyncMock()

    with (
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=mock_sc,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_contradiction_check",
            side_effect=_judge,
        ),
    ):
        await _detect(new, [0.1] * VECTOR_DIM)

    assert mock_sc.update_memory_status.call_count == 0
    assert mock_sc.batch_update_status.call_count == 1
    payload = mock_sc.batch_update_status.call_args.args[0]
    # One verdict ⇒ 1 candidate-conflicted row + 1 new_memory row.
    assert len(payload["updates"]) == 2
