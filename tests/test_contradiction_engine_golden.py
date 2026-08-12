"""Golden differential test for the contradiction engine consolidation (A55).

PURPOSE — no-degradation guard for the "engine" refactor. This test freezes
TODAY's contradiction-detection *outcomes* (which memory ends up
``outdated``/``conflicted``/``active`` and where ``supersedes_id`` points) for a
representative set of scenarios into a committed fixture
(``tests/fixtures/contradiction_golden_v1.json``). The refactor that moves the
detector behind ``ContradictionEngine.evaluate_async`` must reproduce this
fixture byte-for-byte.

The refactor changes exactly TWO lines here — ``_invoke_path_a`` and
``_invoke_path_c`` (the single seam). Today they call the current entry points
(``_detect`` / ``detect_contradictions_by_entities_async``). After Phase 1 they
call ``engine.evaluate_async(..., trigger=...)``. The GOLDEN stays identical; if
any outcome or ordering drifts, this test fails.

Regenerate the fixture (only when a change is *intended*): ``GEN_GOLDEN=1 pytest
tests/test_contradiction_engine_golden.py`` then review the diff.

Note: LLM verdicts and the storage layer are mocked (same seams the existing
887 contradiction tests use). Gate-1/Gate-2 and provider-fallback behaviour is
pinned by their own dedicated tests; here we lock the *resolution* outcome given
a verdict, across every path.
"""

from __future__ import annotations

import json
import os
import pathlib
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from core_api.constants import VECTOR_DIM

from tests._contradiction_batch_compat import install_batch_status_replay_shim

pytestmark = pytest.mark.unit

_FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "contradiction_golden_v1.json"

# Stable ids so role-mapping is deterministic across runs.
NEW_ID = "11111111-1111-1111-1111-111111111111"
CAND_ID = "22222222-2222-2222-2222-222222222222"
SUBJECT = "00000000-0000-0000-0000-0000000000aa"


# ---------------------------------------------------------------------------
# Fixture builders (shaped like what storage hands the detector)
# ---------------------------------------------------------------------------
def _new_mem(*, ts: str, object_value: str, supersedes_id: str | None = None) -> dict:
    return {
        "id": NEW_ID,
        "tenant_id": "t1",
        "fleet_id": "f1",
        "content": f"X lives in {object_value}",
        "subject_entity_id": SUBJECT,
        "predicate": "lives_in",
        "object_value": object_value,
        "deleted_at": None,
        "status": "active",
        "visibility": "scope_team",
        "supersedes_id": supersedes_id,
        "created_at": ts,
    }


def _cand(*, ts: str, object_value: str, status: str = "active") -> dict:
    return {
        "id": CAND_ID,
        "tenant_id": "t1",
        "fleet_id": "f1",
        "content": f"X lives in {object_value}",
        "subject_entity_id": SUBJECT,
        "predicate": "lives_in",
        "object_value": object_value,
        "status": status,
        "supersedes_id": None,
        "deleted_at": None,
        "visibility": "scope_team",
        "created_at": ts,
    }


_ROLE = {NEW_ID: "new", CAND_ID: "cand"}


def _normalize(mock_sc: AsyncMock) -> list[list]:
    """Turn the captured status writes into an id-independent, sorted outcome:
    a sorted list of ``[role, status, supersedes_role_or_marker]``."""
    out: list[list] = []
    for c in mock_sc.update_memory_status.call_args_list:
        mid, status = c.args[0], c.args[1]
        if c.kwargs.get("unset_supersedes"):
            sup = "UNSET"
        elif c.kwargs.get("supersedes_id") is not None:
            sup = _ROLE.get(c.kwargs["supersedes_id"], "other")
        else:
            sup = None
        out.append([_ROLE.get(mid, "other"), status, sup])
    return sorted(out)


# ---------------------------------------------------------------------------
# THE SEAM — the only lines the Phase-1 refactor re-points at the engine.
# ---------------------------------------------------------------------------
async def _invoke_path_a(new_mem: dict) -> None:
    from core_api.services.contradiction_detector import _detect

    await _detect(new_mem, [0.1] * VECTOR_DIM)


async def _invoke_path_c(new_id: str) -> None:
    from core_api.services.contradiction_detector import (
        detect_contradictions_by_entities_async,
    )

    await detect_contradictions_by_entities_async(new_id, "t1", "f1")


# ---------------------------------------------------------------------------
# Scenario runners — each returns a normalized outcome
# ---------------------------------------------------------------------------
def _base_sc() -> AsyncMock:
    sc = AsyncMock()
    sc.find_rdf_conflicts = AsyncMock(return_value=[])
    sc.find_similar_candidates = AsyncMock(return_value=[])
    sc.find_entity_overlap_candidates = AsyncMock(return_value=[])
    sc.update_memory_status = AsyncMock()
    install_batch_status_replay_shim(sc)
    return sc


async def _run_path_a_rdf(*, cand_older: bool) -> list[list]:
    if cand_older:
        new = _new_mem(ts="2026-04-29T12:00:00+00:00", object_value="Haifa")
        cand = _cand(ts="2026-04-29T10:00:00+00:00", object_value="Tel Aviv")
    else:
        new = _new_mem(ts="2026-04-29T10:00:00+00:00", object_value="Haifa")
        cand = _cand(ts="2026-04-29T12:00:00+00:00", object_value="Tel Aviv")
    sc = _base_sc()
    sc.find_rdf_conflicts = AsyncMock(return_value=[cand])
    with patch(
        "core_api.services.contradiction_detector.get_storage_client", return_value=sc
    ):
        await _invoke_path_a(new)
    return _normalize(sc)


async def _run_path_a_semantic(*, verdict: bool) -> list[list]:
    new = _new_mem(ts="2026-04-29T12:00:00+00:00", object_value="Haifa")
    cand = _cand(ts="2026-04-29T10:00:00+00:00", object_value="Tel Aviv")
    sc = _base_sc()
    sc.find_similar_candidates = AsyncMock(return_value=[cand])
    with (
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_contradiction_check",
            new_callable=AsyncMock,
            return_value=(verdict, 0.90),
        ),
    ):
        await _invoke_path_a(new)
    return _normalize(sc)


def _sc_for_path_c(
    *, cand_status: str, overlap: bool, new_supersedes: str | None
) -> AsyncMock:
    new_mem = _new_mem(
        ts="2026-04-29T12:00:00+00:00",
        object_value="Haifa",
        supersedes_id=new_supersedes,
    )
    cand_mem = _cand(
        ts="2026-04-29T10:00:00+00:00", object_value="Tel Aviv", status=cand_status
    )
    sc = _base_sc()

    async def get_memory(mid: str) -> dict | None:
        return {NEW_ID: new_mem, CAND_ID: cand_mem}.get(mid)

    sc.get_memory = AsyncMock(side_effect=get_memory)
    if overlap:
        sc.find_entity_overlap_candidates = AsyncMock(return_value=[cand_mem])
    ent_new, ent_cand = str(uuid4()), str(uuid4())
    sc.get_entity_links_for_memories = AsyncMock(
        return_value={
            NEW_ID: [{"entity_id": ent_new, "role": "subject"}],
            CAND_ID: [{"entity_id": ent_cand, "role": "subject"}],
        }
    )

    async def get_entity(eid: str) -> dict | None:
        if eid == ent_new:
            return {"id": eid, "canonical_name": "Haifa", "entity_type": "place"}
        if eid == ent_cand:
            return {"id": eid, "canonical_name": "Tel Aviv", "entity_type": "place"}
        return None

    sc.get_entity = AsyncMock(side_effect=get_entity)
    return sc


async def _run_path_c(*, verdict: bool, retraction: bool) -> list[list]:
    # retraction scenario: new already supersedes cand (a prior Path-A verdict),
    # cand is outdated, no fresh overlap — only the retraction re-judge runs.
    # Retraction only fires on a candidate still in the ``conflicted`` state
    # Path A produced (see _attempt_path_c_retraction guard).
    sc = _sc_for_path_c(
        cand_status="conflicted" if retraction else "active",
        overlap=not retraction,
        new_supersedes=CAND_ID if retraction else None,
    )
    with (
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_entity_aware_contradiction_check",
            new_callable=AsyncMock,
            return_value=(verdict, 0.90),
        ),
        patch(
            "core_api.services.contradiction_detector.resolve_config",
            new_callable=AsyncMock,
            return_value=None,
            create=True,
        ),
    ):
        await _invoke_path_c(NEW_ID)
    return _normalize(sc)


# name -> async factory
SCENARIOS = {
    "path_a_rdf_cand_older": lambda: _run_path_a_rdf(cand_older=True),
    "path_a_rdf_cand_newer": lambda: _run_path_a_rdf(cand_older=False),
    "path_a_semantic_conflict": lambda: _run_path_a_semantic(verdict=True),
    "path_a_semantic_no_conflict": lambda: _run_path_a_semantic(verdict=False),
    "path_c_forward_conflict": lambda: _run_path_c(verdict=True, retraction=False),
    "path_c_forward_no_conflict": lambda: _run_path_c(verdict=False, retraction=False),
    "path_c_retraction": lambda: _run_path_c(verdict=False, retraction=True),
}


@pytest.mark.asyncio
async def test_contradiction_golden_differential():
    """Every scenario's resolution outcome matches the frozen golden.

    Set GEN_GOLDEN=1 to regenerate the fixture from current behaviour.
    """
    captured = {name: await factory() for name, factory in SCENARIOS.items()}

    if os.getenv("GEN_GOLDEN"):
        _FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        _FIXTURE.write_text(json.dumps(captured, indent=2, sort_keys=True) + "\n")
        pytest.skip(f"regenerated golden fixture at {_FIXTURE}")

    assert _FIXTURE.exists(), (
        f"missing golden fixture {_FIXTURE}; run with GEN_GOLDEN=1"
    )
    golden = json.loads(_FIXTURE.read_text())

    # Compare per-scenario for a readable diff on failure.
    for name in SCENARIOS:
        assert captured[name] == golden.get(name), (
            f"contradiction outcome drift in '{name}':\n"
            f"  golden  = {golden.get(name)}\n"
            f"  current = {captured[name]}"
        )
    assert set(captured) == set(golden), "scenario set changed vs golden fixture"
