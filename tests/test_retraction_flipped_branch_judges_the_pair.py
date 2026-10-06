"""Path C's flipped-direction retraction must judge the two rows of the chain.

In a flipped verdict the row Path C is processing is the LOSER: another row
(the edge owner) carries ``supersedes_id`` pointing at it. The retraction
re-judge used to read the "new statement" side from the processed row in both
directions, so in the flipped branch it asked whether the loser contradicts
itself. The judge answers that "no" at full confidence, which cleared the
retraction threshold and revived the superseded fact every time.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from core_api.services import contradiction_detector as cd

pytestmark = pytest.mark.asyncio


def _mem(mid: str, *, content: str, status: str, supersedes_id: str | None) -> dict:
    return {
        "id": mid,
        "tenant_id": "t1",
        "fleet_id": "f1",
        "content": content,
        "status": status,
        "visibility": "scope_team",
        "supersedes_id": supersedes_id,
        "deleted_at": None,
    }


def _flipped_chain():
    loser_id, owner_id = str(uuid4()), str(uuid4())
    loser = _mem(
        loser_id, content="Alice lives in Boston", status="outdated", supersedes_id=None
    )
    owner = _mem(
        owner_id, content="Alice moved to NYC", status="active", supersedes_id=loser_id
    )
    return loser, owner


def _sc(owner: dict) -> AsyncMock:
    sc = AsyncMock()
    sc.find_by_supersedes_id = AsyncMock(return_value=[owner])
    return sc


def _contexts(loser: dict, owner: dict):
    by_id = {
        owner["id"]: [{"canonical_name": "alice", "role": "subject", "src": "owner"}],
        loser["id"]: [{"canonical_name": "alice", "role": "subject", "src": "loser"}],
    }

    async def fetch(_sc, ids, _tenant):
        return {i: by_id[i] for i in ids}

    return fetch


async def test_the_flipped_branch_judges_the_owner_against_the_loser() -> None:
    loser, owner = _flipped_chain()
    sc = _sc(owner)
    judge = AsyncMock(return_value=(True, cd._CONF_CLEAN))

    with (
        patch.object(cd, "_fetch_entity_contexts", _contexts(loser, owner)),
        patch.object(cd, "_llm_entity_aware_contradiction_check", judge),
    ):
        retracted = await cd._attempt_entity_retraction(sc, loser, SimpleNamespace())

    assert retracted is False  # the judge agreed with the verdict
    new_content, old_content, new_entities, old_entities, _cfg = judge.await_args.args
    assert new_content == owner["content"]
    assert old_content == loser["content"]
    assert new_entities[0]["src"] == "owner"
    assert old_entities[0]["src"] == "loser"


async def test_a_flipped_verdict_the_judge_agrees_with_is_not_retracted() -> None:
    """End to end on the branch: no status writes when the real pair still
    contradicts. Before the fix the self-comparison drove a retraction here."""
    loser, owner = _flipped_chain()
    sc = _sc(owner)

    async def judge(new_content, old_content, *_rest):
        # A stand-in judge that answers the way a real one does: a statement
        # never contradicts itself; these two do.
        if new_content == old_content:
            return False, cd._CONF_CLEAN
        return True, cd._CONF_CLEAN

    with (
        patch.object(cd, "_fetch_entity_contexts", _contexts(loser, owner)),
        patch.object(cd, "_llm_entity_aware_contradiction_check", judge),
    ):
        retracted = await cd._attempt_entity_retraction(sc, loser, SimpleNamespace())

    assert retracted is False
    sc.update_memory_status.assert_not_awaited()


async def test_a_self_edge_is_refused_without_a_judge_call() -> None:
    mid = str(uuid4())
    row = _mem(mid, content="x", status="outdated", supersedes_id=None)
    self_owner = dict(row, supersedes_id=mid)
    sc = _sc(self_owner)
    judge = AsyncMock()

    with patch.object(cd, "_llm_entity_aware_contradiction_check", judge):
        assert await cd._attempt_entity_retraction(sc, row, SimpleNamespace()) is False

    judge.assert_not_awaited()
