"""Path C's judges keep their fallback, and retraction leaves RDF verdicts alone.

L-179: ``call_with_fallback`` already bounds each judge attempt at 10 s and runs
a retry, then the fallback provider. Path C's single-candidate judge and the
retraction judge were also wrapped in ``asyncio.wait_for(timeout=10.0)``, so a
hanging primary hit both ceilings at the same moment and the outer one
cancelled the chain before the retry or the fallback could run. Path A and the
batched branch await the chain directly; these two now do too.

L-27: retraction re-judged any ``outdated`` loser, including the deterministic
RDF pass's verdicts (one subject, one single-value attribute, two different
values). Nothing there is the judge's to decide, and when the judge disagreed
the RDF pass re-applied the verdict moments later, so each retraction only
churned status writes and counted the conflict again.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from core_api.services import contradiction_detector as cd

pytestmark = pytest.mark.asyncio


def _record_wrapped(monkeypatch) -> list[str]:
    """Names of the coroutines handed to ``asyncio.wait_for`` from now on."""
    wrapped: list[str] = []
    real = asyncio.wait_for

    async def spy(aw, *args, **kwargs):
        wrapped.append(getattr(getattr(aw, "cr_code", None), "co_name", ""))
        return await real(aw, *args, **kwargs)

    monkeypatch.setattr(asyncio, "wait_for", spy)
    return wrapped


def _mem(
    mid: str, *, content: str, status: str, supersedes_id: str | None, **triple
) -> dict:
    return {
        "id": mid,
        "tenant_id": "t1",
        "fleet_id": "f1",
        "content": content,
        "status": status,
        "visibility": "scope_team",
        "supersedes_id": supersedes_id,
        "deleted_at": None,
        "subject_entity_id": triple.get("subject"),
        "predicate": triple.get("predicate"),
        "object_value": triple.get("value"),
    }


def _flipped_chain(loser_triple: dict | None = None, owner_triple: dict | None = None):
    loser_id, owner_id = str(uuid4()), str(uuid4())
    loser = _mem(
        loser_id,
        content="Project Kite is in beta",
        status="outdated",
        supersedes_id=None,
        **(loser_triple or {}),
    )
    owner = _mem(
        owner_id,
        content="Project Kite has shipped",
        status="active",
        supersedes_id=loser_id,
        **(owner_triple or {}),
    )
    return loser, owner


def _contexts(loser: dict, owner: dict):
    by_id = {
        owner["id"]: [{"canonical_name": "kite", "role": "subject"}],
        loser["id"]: [{"canonical_name": "kite", "role": "subject"}],
    }

    async def fetch(_sc, ids, _tenant):
        return {i: by_id[i] for i in ids}

    return fetch


def _owner_lookup(owner: dict) -> AsyncMock:
    sc = AsyncMock()
    sc.find_by_supersedes_id = AsyncMock(return_value=[owner])
    return sc


# --- L-179: no outer cut on the judge chain ----------------------------------


async def test_the_retraction_judge_is_not_cut_short(monkeypatch) -> None:
    loser, owner = _flipped_chain()
    wrapped = _record_wrapped(monkeypatch)

    async def judge(*_args):
        return True, cd._CONF_CLEAN

    with (
        patch.object(cd, "_fetch_entity_contexts", _contexts(loser, owner)),
        patch.object(cd, "_llm_entity_aware_contradiction_check", judge),
    ):
        retracted = await cd._attempt_entity_retraction(
            _owner_lookup(owner), loser, SimpleNamespace()
        )

    assert retracted is False
    assert "judge" not in wrapped, f"the judge chain was wrapped in wait_for: {wrapped}"


async def test_path_c_single_candidate_judge_is_not_cut_short(monkeypatch) -> None:
    new_id, cand_id = str(uuid4()), str(uuid4())
    new_mem = _mem(new_id, content="X is in Haifa", status="active", supersedes_id=None)
    new_mem["created_at"] = "2026-05-24T12:00:00+00:00"
    cand = _mem(cand_id, content="X is in Acre", status="active", supersedes_id=None)
    cand["created_at"] = "2026-05-23T12:00:00+00:00"

    async def get_memory(mid: str, tenant_id: str, **_kw):
        return {new_id: new_mem, cand_id: cand}.get(mid)

    sc = AsyncMock()
    sc.get_memory = AsyncMock(side_effect=get_memory)
    sc.find_entity_overlap_candidates = AsyncMock(return_value=[cand])
    sc.get_entity_links_for_memories = AsyncMock(return_value={})
    sc.batch_update_status = AsyncMock(return_value={"ok": True, "skipped": []})
    wrapped = _record_wrapped(monkeypatch)

    async def judge(*_args):
        return True, 0.95

    with (
        patch.object(cd, "get_storage_client", return_value=sc),
        patch.object(cd, "_llm_contradiction_check", judge),
        patch.object(
            cd,
            "resolve_config",
            new_callable=AsyncMock,
            return_value=None,
            create=True,
        ),
        patch.object(
            cd, "_acquire_entity_lock", new_callable=AsyncMock, return_value=True
        ),
    ):
        await cd.detect_contradictions_by_entities_async(new_id, "t1", "f1")

    assert "judge" not in wrapped, f"the judge chain was wrapped in wait_for: {wrapped}"
    # The verdict still lands: the run marked the candidate.
    sc.batch_update_status.assert_awaited()


# --- L-27: RDF verdicts are not re-judged -------------------------------------


@pytest.mark.parametrize(
    ("loser_value", "owner_value"),
    [("beta", "shipped"), ("7,500 rpm", "8000 RPM")],
)
async def test_an_rdf_value_conflict_is_not_rejudged(loser_value, owner_value) -> None:
    loser, owner = _flipped_chain(
        {"subject": "ent-kite", "predicate": "status", "value": loser_value},
        {"subject": "ent-kite", "predicate": "Status", "value": owner_value},
    )
    sc = _owner_lookup(owner)
    judge = AsyncMock(return_value=(False, cd._CONF_CLEAN))

    with (
        patch.object(cd, "_fetch_entity_contexts", _contexts(loser, owner)),
        patch.object(cd, "_llm_entity_aware_contradiction_check", judge),
    ):
        retracted = await cd._attempt_entity_retraction(sc, loser, SimpleNamespace())

    assert retracted is False
    judge.assert_not_awaited()
    sc.update_memory_status.assert_not_awaited()


@pytest.mark.parametrize(
    ("loser_triple", "owner_triple"),
    [
        # Same value once case, spaces and thousands separators go: not an RDF
        # conflict, so the verdict came from the content judge.
        (
            {"subject": "ent-kite", "predicate": "status", "value": "In Beta"},
            {"subject": "ent-kite", "predicate": "status", "value": "in  beta"},
        ),
        # A multi-value predicate: the RDF pass never marks it.
        (
            {"subject": "ent-kite", "predicate": "uses_tool", "value": "jira"},
            {"subject": "ent-kite", "predicate": "uses_tool", "value": "linear"},
        ),
        # Different subjects.
        (
            {"subject": "ent-kite", "predicate": "status", "value": "beta"},
            {"subject": "ent-hawk", "predicate": "status", "value": "shipped"},
        ),
        # No triple at all.
        (None, None),
    ],
)
async def test_a_content_verdict_is_still_rejudged(loser_triple, owner_triple) -> None:
    loser, owner = _flipped_chain(loser_triple, owner_triple)
    sc = _owner_lookup(owner)
    judge = AsyncMock(return_value=(True, cd._CONF_CLEAN))

    with (
        patch.object(cd, "_fetch_entity_contexts", _contexts(loser, owner)),
        patch.object(cd, "_llm_entity_aware_contradiction_check", judge),
    ):
        await cd._attempt_entity_retraction(sc, loser, SimpleNamespace())

    judge.assert_awaited_once()


@pytest.mark.parametrize(
    ("loser_update", "owner_update"),
    [
        # The pass's gate lower-cases the new memory's predicate; it does not strip.
        ({"predicate": " status "}, {}),
        # The query lower-cases the stored predicate; it does not strip either.
        ({}, {"predicate": " Status "}),
        # The query's scope: same fleet, same visibility, same owning agent for
        # scope_agent, and a live row.
        ({}, {"fleet_id": "f2"}),
        ({}, {"visibility": "scope_org"}),
        (
            {"visibility": "scope_agent", "agent_id": "agent-a"},
            {"visibility": "scope_agent", "agent_id": "agent-b"},
        ),
        ({}, {"status": "archived"}),
    ],
)
async def test_a_pair_the_rdf_pass_would_skip_is_still_rejudged(
    loser_update, owner_update
) -> None:
    """Blocking a retraction the RDF pass would not undo leaves the verdict
    standing for good, so the check must match the pass term by term."""
    loser, owner = _flipped_chain(
        {"subject": "ent-kite", "predicate": "status", "value": "beta"},
        {"subject": "ent-kite", "predicate": "status", "value": "shipped"},
    )
    loser.update(loser_update)
    owner.update(owner_update)
    sc = _owner_lookup(owner)
    judge = AsyncMock(return_value=(True, cd._CONF_CLEAN))

    with (
        patch.object(cd, "_fetch_entity_contexts", _contexts(loser, owner)),
        patch.object(cd, "_llm_entity_aware_contradiction_check", judge),
    ):
        await cd._attempt_entity_retraction(sc, loser, SimpleNamespace())

    judge.assert_awaited_once()


async def test_a_canonical_rdf_verdict_is_not_rejudged() -> None:
    """Canonical: the new memory won and the loser is the row the pass finds once
    retraction reverts it, so the loser's demoted status must not count against
    the match."""
    loser_id = str(uuid4())
    loser = _mem(
        loser_id,
        content="Project Kite is in beta",
        status="conflicted",
        supersedes_id=None,
        subject="ent-kite",
        predicate="status",
        value="beta",
    )
    winner = _mem(
        str(uuid4()),
        content="Project Kite has shipped",
        status="active",
        supersedes_id=loser_id,
        subject="ent-kite",
        predicate="status",
        value="shipped",
    )
    sc = AsyncMock()
    sc.get_memory = AsyncMock(return_value=loser)
    judge = AsyncMock(return_value=(False, cd._CONF_CLEAN))

    with (
        patch.object(cd, "_fetch_entity_contexts", _contexts(loser, winner)),
        patch.object(cd, "_llm_entity_aware_contradiction_check", judge),
    ):
        retracted = await cd._attempt_entity_retraction(sc, winner, SimpleNamespace())

    assert retracted is False
    judge.assert_not_awaited()
    sc.update_memory_status.assert_not_awaited()
