"""Detector -> conflict-record wiring (A55 1d), flag-gated.

Proves: with contradiction_write_conflict_record ON, a confirmed conflict in
``_detect`` schedules a memory_conflicts record; with it OFF (default) it does
not. Either way the status/supersedes effect is unchanged (no degradation).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from core_api.constants import VECTOR_DIM
from core_api.services.contradiction_detector import _detect

from tests._contradiction_batch_compat import install_batch_status_replay_shim

pytestmark = pytest.mark.unit

SUBJ = "00000000-0000-0000-0000-0000000000aa"


def _new(object_value: str, ts: str) -> dict:
    return {
        "id": str(uuid4()),
        "tenant_id": "t1",
        "fleet_id": "f1",
        "content": f"X lives in {object_value}",
        "subject_entity_id": SUBJ,
        "predicate": "lives_in",
        "object_value": object_value,
        "deleted_at": None,
        "status": "active",
        "visibility": "scope_team",
        "supersedes_id": None,
        "created_at": ts,
    }


def _cand(object_value: str, ts: str) -> dict:
    return {
        "id": str(uuid4()),
        "content": f"X lives in {object_value}",
        "status": "active",
        "object_value": object_value,
        "created_at": ts,
    }


def _rdf_conflict_sc() -> AsyncMock:
    cand = _cand("Tel Aviv", "2026-04-29T10:00:00+00:00")
    sc = AsyncMock()
    sc.find_rdf_conflicts = AsyncMock(return_value=[cand])
    sc.find_similar_candidates = AsyncMock(return_value=[])
    sc.update_memory_status = AsyncMock()
    install_batch_status_replay_shim(sc)
    return sc, cand


async def _run(flag: bool):
    new = _new("Haifa", "2026-04-29T12:00:00+00:00")
    sc, cand = _rdf_conflict_sc()
    rec = AsyncMock()
    with (
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.contradiction.resolver.record_detected_conflicts",
            rec,
        ),
        patch("core_api.config.settings.contradiction_write_conflict_record", flag),
    ):
        await _detect(new, [0.1] * VECTOR_DIM)
    return new, cand, sc, rec


@pytest.mark.asyncio
async def test_flag_on_records_conflict_and_preserves_effect():
    new, cand, sc, rec = await _run(True)

    # Record path fired once, with the RDF pair.
    rec.assert_awaited_once()
    pairs = rec.await_args.args[1]
    assert pairs[0][0]["id"] == cand["id"] and pairs[0][1] == "rdf"
    assert rec.await_args.kwargs["tenant_id"] == "t1"

    # Effect unchanged: candidate marked outdated, new supersedes it.
    calls = [c.args for c in sc.update_memory_status.call_args_list]
    assert (cand["id"], "outdated") in calls
    new_call = [
        c
        for c in sc.update_memory_status.call_args_list
        if c.args and c.args[0] == new["id"]
    ]
    assert new_call and new_call[0].kwargs.get("supersedes_id") == cand["id"]


@pytest.mark.asyncio
async def test_flag_off_does_not_record_but_still_applies_effect():
    _, cand, sc, rec = await _run(False)

    rec.assert_not_awaited()  # no record write when flag is off (default)
    # Effect still applied — identical to today.
    calls = [c.args for c in sc.update_memory_status.call_args_list]
    assert (cand["id"], "outdated") in calls
