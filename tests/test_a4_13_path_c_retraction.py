"""A4 #13 — Path C retracts a wrong Path A verdict via the A4 #10 primitive.

Context
───────
Path A runs first (post-commit, semantic similarity). It can wrongly
flag a contradiction when two memories *look* similar but are actually
about different real-world subjects (e.g. two people sharing a first
name; a project name reused by two teams). Once flagged, the older
candidate is marked ``conflicted`` and the new memory carries
``supersedes_id`` pointing back at it.

Path C runs later (post-entity-extraction). Now there's MORE
information — the entity extractor has identified actual subjects
(``MemoryEntityLink`` rows). A4 #13 has Path C re-judge the candidate
Path A retracted, using ``_llm_contradiction_check`` (A4 #12 — returns
``(verdict, confidence)``). If the verdict is now NOT-a-contradiction
with sufficient confidence, Path C retracts Path A's verdict via the
A4 #10 storage primitive (``unset_supersedes=True`` + CAS).

Chain shape on entry to Path C (after wrong Path A verdict):
  candidate.status = "conflicted",  candidate.supersedes_id = NULL
  new_memory.status = "active",      new_memory.supersedes_id = candidate.id

Retraction performs (atomically from the API caller's POV):
  candidate.status      → "active"
  new_memory.supersedes_id → NULL  (CAS guarded by expected=candidate.id)

Confidence rubric (A4 #12):
  0.90 — clean LLM agreement (both gates aligned)
  0.85 — gate 2 fired (model named a non_conflict_reason)
  0.60 — gate 1 fired (model said contradicts=True with same_subject=False)
  0.50 — malformed / heuristic fallback

CAURA-128 — threshold tightened from 0.60 to 0.90. The original
retraction judge called ``_llm_contradiction_check`` with the SAME
inputs as Path A's semantic judge; there was no extra entity context.
Independent rolls of the same LLM call disagreed non-deterministically
on synthetic / unusual subjects, and at threshold 0.60 the
parser-override gate (gate 1) silently retracted genuine Path A
verdicts. Wet-tested on net 2026-05-26 via
``scripts/repro_contradictions_race.py``.

CAURA-129 — the retraction judge now calls
``_llm_entity_aware_contradiction_check``, which renders the resolved
``MemoryEntityLink`` rows for both memories into the prompt (canonical
names + entity_type + role). The judge answers a structurally different
question than Path A — grounded on entity identity rather than raw-text
NER. Retraction is also skipped when either memory has no resolved
entity_links (degenerate input → Path A's verdict stands; see the
empty-context tests below). Threshold remains at ``_CONF_CLEAN`` (0.90)
for the initial PR; revisit after production signal.

Why direct lookup (not via A4 #11):
A4 #11's ``include_supersedes=True`` filter was structurally inverted
relative to Path A's actual chain shape (see
[[flow-debug-contradiction-chain-shape]]). Path A leaves
``older.supersedes_id`` as NULL; the filter expected
``older.supersedes_id == new_memory.id``, which is never written. A4
#13 sidesteps the broken filter by dereferencing
``new_memory.supersedes_id`` directly — one extra ``sc.get_memory(...)``
call, works in both canonical and flipped Path A directions. Fixing or
removing the A4 #11 filter is tracked separately
(see [[followup-a4-11-filter-dead]]).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from tests._contradiction_batch_compat import install_batch_status_replay_shim


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_memory(
    mid: UUID, *, status: str, supersedes_id: UUID | None, content: str = "content"
) -> dict:
    return {
        "id": str(mid),
        "tenant_id": "t1",
        "fleet_id": "f1",
        "agent_id": "a1",
        "content": content,
        "status": status,
        "visibility": "scope_team",
        "supersedes_id": str(supersedes_id) if supersedes_id else None,
        "deleted_at": None,
        "created_at": "2026-05-24T10:00:00+00:00",
    }


def _mock_sc_with_retraction_setup(
    new_id: UUID, cand_id: UUID, *, cand_status: str = "conflicted"
) -> AsyncMock:
    """Build a mock storage client where Path A has already retracted ``cand``
    by ``new``: new.supersedes_id=cand, cand.status='conflicted'."""
    sc = AsyncMock()
    new_mem = _make_memory(
        new_id,
        status="active",
        supersedes_id=cand_id,
        content="new statement about subject X",
    )
    cand_mem = _make_memory(
        cand_id,
        status=cand_status,
        supersedes_id=None,
        content="old statement that looked similar but is about subject Y",
    )

    async def get_memory(mid: str) -> dict | None:
        if mid == str(new_id):
            return new_mem
        if mid == str(cand_id):
            return cand_mem
        return None

    sc.get_memory = AsyncMock(side_effect=get_memory)
    sc.find_entity_overlap_candidates = AsyncMock(return_value=[])
    sc.update_memory_status = AsyncMock()
    # CAURA-129 — Path C retraction now fetches entity context for both
    # memories before the judge fires. Default mock returns one resolved
    # link per memory so existing tests stay on the same code path; the
    # empty-links guard is exercised by dedicated tests below.
    _ent_new = str(uuid4())
    _ent_cand = str(uuid4())
    sc.get_entity_links_for_memories = AsyncMock(
        return_value={
            str(new_id): [{"entity_id": _ent_new, "role": "subject"}],
            str(cand_id): [{"entity_id": _ent_cand, "role": "subject"}],
        }
    )

    async def get_entity(eid: str) -> dict | None:
        if eid == _ent_new:
            return {"id": eid, "canonical_name": "Project X", "entity_type": "project"}
        if eid == _ent_cand:
            return {"id": eid, "canonical_name": "Project Y", "entity_type": "project"}
        return None

    sc.get_entity = AsyncMock(side_effect=get_entity)
    install_batch_status_replay_shim(sc)
    return sc


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Retraction fires — clean (False, 0.90) and gate-2 (False, 0.85)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retracts_when_judge_says_not_contradiction_clean_confidence():
    """Path A flagged contradiction; Path C's judge says NOT contradiction
    with confidence 0.90 (clean LLM agreement, both gates aligned).
    Retraction MUST fire: candidate back to ``active``, new memory's
    ``supersedes_id`` cleared."""
    from core_api.services.contradiction_detector import (
        detect_contradictions_by_entities_async,
    )

    new_id, cand_id = uuid4(), uuid4()
    sc = _mock_sc_with_retraction_setup(new_id, cand_id)

    with (
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_entity_aware_contradiction_check",
            new_callable=AsyncMock,
            return_value=(False, 0.90),
        ),
        patch(
            "core_api.services.contradiction_detector.resolve_config",
            new_callable=AsyncMock,
            return_value=None,
            create=True,
        ),
    ):
        await detect_contradictions_by_entities_async(new_id, "t1", "f1")

    calls = sc.update_memory_status.call_args_list
    # 1) Candidate reverted to active.
    assert any(c.args == (str(cand_id), "active") for c in calls), (
        f"expected candidate reverted to active; calls={calls}"
    )
    # 2) New memory's supersedes_id cleared via A4 #10 (CAS by expected).
    clear_calls = [
        c
        for c in calls
        if c.args
        and c.args[0] == str(new_id)
        and c.kwargs.get("unset_supersedes") is True
        and c.kwargs.get("expected_supersedes_id") == str(cand_id)
    ]
    assert len(clear_calls) == 1, (
        f"expected exactly one supersedes-clear call on new memory with CAS "
        f"anchor={cand_id}; got {calls}"
    )


@pytest.mark.asyncio
async def test_no_retraction_at_gate2_below_threshold():
    """CAURA-128 — Gate 2 fired (verdict=False, confidence 0.85) USED to
    retract under the original threshold 0.60. The judge call is the
    same LLM prompt + same inputs as Path A's semantic judge — no extra
    entity context is passed — so a 0.85 disagreement is just a single
    independent roll flipping. We tightened the floor to 0.90; gate-2
    alone is no longer sufficient to silently revert Path A.

    Wet-test evidence: scripts/repro_contradictions_race.py on net
    2026-05-26 — see CAURA-128 PR body."""
    from core_api.services.contradiction_detector import (
        detect_contradictions_by_entities_async,
    )

    new_id, cand_id = uuid4(), uuid4()
    sc = _mock_sc_with_retraction_setup(new_id, cand_id)

    with (
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_entity_aware_contradiction_check",
            new_callable=AsyncMock,
            return_value=(False, 0.85),
        ),
        patch(
            "core_api.services.contradiction_detector.resolve_config",
            new_callable=AsyncMock,
            return_value=None,
            create=True,
        ),
    ):
        await detect_contradictions_by_entities_async(new_id, "t1", "f1")

    revert_calls = [
        c
        for c in sc.update_memory_status.call_args_list
        if c.args == (str(cand_id), "active")
    ]
    assert revert_calls == [], (
        f"verdict=False at 0.85 must NOT retract under the tightened "
        f"CAURA-128 threshold (0.90); got {revert_calls}"
    )


@pytest.mark.asyncio
async def test_no_retraction_on_gate1_stochastic_signal():
    """CAURA-128 regression test — Gate 1 (verdict=False, confidence
    0.60) is the **stochastic flip case**: model said
    ``contradicts=True`` with ``same_subject=False`` and the parser
    overrode to False. Under the original threshold 0.60 this silently
    retracted genuine Path A flags on memclaw.net. Now it must NOT
    retract."""
    from core_api.services.contradiction_detector import (
        detect_contradictions_by_entities_async,
    )

    new_id, cand_id = uuid4(), uuid4()
    sc = _mock_sc_with_retraction_setup(new_id, cand_id)

    with (
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_entity_aware_contradiction_check",
            new_callable=AsyncMock,
            return_value=(False, 0.60),
        ),
        patch(
            "core_api.services.contradiction_detector.resolve_config",
            new_callable=AsyncMock,
            return_value=None,
            create=True,
        ),
    ):
        await detect_contradictions_by_entities_async(new_id, "t1", "f1")

    revert_calls = [
        c
        for c in sc.update_memory_status.call_args_list
        if c.args == (str(cand_id), "active")
    ]
    assert revert_calls == [], (
        f"verdict=False at 0.60 (gate-1 stochastic flip) must NOT retract "
        f"under the tightened CAURA-128 threshold (0.90); got {revert_calls}"
    )


# ---------------------------------------------------------------------------
# Retraction does NOT fire — low confidence, judge agrees with Path A,
# nothing to retract.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_retraction_on_malformed_fallback_confidence():
    """Confidence 0.50 = heuristic fallback / malformed LLM. We can't
    trust verdict=False from a malformed response; leave Path A's call
    alone."""
    from core_api.services.contradiction_detector import (
        detect_contradictions_by_entities_async,
    )

    new_id, cand_id = uuid4(), uuid4()
    sc = _mock_sc_with_retraction_setup(new_id, cand_id)

    with (
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_entity_aware_contradiction_check",
            new_callable=AsyncMock,
            return_value=(False, 0.50),
        ),
        patch(
            "core_api.services.contradiction_detector.resolve_config",
            new_callable=AsyncMock,
            return_value=None,
            create=True,
        ),
    ):
        await detect_contradictions_by_entities_async(new_id, "t1", "f1")

    # No revert-to-active call on the candidate.
    revert_calls = [
        c
        for c in sc.update_memory_status.call_args_list
        if c.args == (str(cand_id), "active")
    ]
    assert revert_calls == [], (
        f"low-confidence verdict=False must NOT retract; got {revert_calls}"
    )


@pytest.mark.asyncio
async def test_no_retraction_when_judge_confirms_contradiction():
    """Judge says verdict=True (e.g. confidence 0.90 — real contradiction).
    Path A was right; Path C MUST leave the retraction alone."""
    from core_api.services.contradiction_detector import (
        detect_contradictions_by_entities_async,
    )

    new_id, cand_id = uuid4(), uuid4()
    sc = _mock_sc_with_retraction_setup(new_id, cand_id)

    with (
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_entity_aware_contradiction_check",
            new_callable=AsyncMock,
            return_value=(True, 0.90),
        ),
        patch(
            "core_api.services.contradiction_detector.resolve_config",
            new_callable=AsyncMock,
            return_value=None,
            create=True,
        ),
    ):
        await detect_contradictions_by_entities_async(new_id, "t1", "f1")

    revert_calls = [
        c
        for c in sc.update_memory_status.call_args_list
        if c.args == (str(cand_id), "active")
    ]
    assert revert_calls == [], (
        f"verdict=True must NOT retract (Path A was right); got {revert_calls}"
    )


# ---------------------------------------------------------------------------
# Pre-conditions — nothing to retract; no-op without judge invocation.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_retraction_when_new_memory_has_no_supersedes_id():
    """Path A didn't retract anything; ``new_memory.supersedes_id`` is
    NULL. The retraction phase must be a fast no-op and MUST NOT call
    the judge."""
    from core_api.services.contradiction_detector import (
        detect_contradictions_by_entities_async,
    )

    new_id = uuid4()
    sc = AsyncMock()
    sc.get_memory = AsyncMock(
        return_value=_make_memory(new_id, status="active", supersedes_id=None)
    )
    sc.find_entity_overlap_candidates = AsyncMock(return_value=[])
    sc.update_memory_status = AsyncMock()
    install_batch_status_replay_shim(sc)

    judge = AsyncMock()
    with (
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_entity_aware_contradiction_check",
            judge,
        ),
        patch(
            "core_api.services.contradiction_detector.resolve_config",
            new_callable=AsyncMock,
            return_value=None,
            create=True,
        ),
    ):
        await detect_contradictions_by_entities_async(new_id, "t1", "f1")

    judge.assert_not_called()
    sc.update_memory_status.assert_not_called()


@pytest.mark.asyncio
async def test_no_retraction_when_candidate_already_active():
    """If the retraction candidate is already ``active`` (someone else
    cleared it between Path A and Path C), the retraction phase MUST
    be a no-op — we don't re-judge a row that no longer needs
    retraction."""
    from core_api.services.contradiction_detector import (
        detect_contradictions_by_entities_async,
    )

    new_id, cand_id = uuid4(), uuid4()
    # Candidate is already active — concurrent writer already retracted.
    sc = _mock_sc_with_retraction_setup(new_id, cand_id, cand_status="active")

    judge = AsyncMock()
    with (
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_entity_aware_contradiction_check",
            judge,
        ),
        patch(
            "core_api.services.contradiction_detector.resolve_config",
            new_callable=AsyncMock,
            return_value=None,
            create=True,
        ),
    ):
        await detect_contradictions_by_entities_async(new_id, "t1", "f1")

    judge.assert_not_called()
    revert_calls = [
        c
        for c in sc.update_memory_status.call_args_list
        if c.args == (str(cand_id), "active")
    ]
    assert revert_calls == []


# ---------------------------------------------------------------------------
# CAURA-129 — empty-entity-context guards. The new entity-aware judge
# has nothing to ground same_subject on if either memory has no resolved
# entity_links; degenerating to raw-content judgement would reintroduce
# the CAURA-128 stochastic-flip class. Empty on either side → skip
# retraction and leave Path A's verdict in place.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_retraction_when_new_memory_has_no_entity_links():
    """CAURA-129 — entity-extraction has not (yet?) populated links on
    the new memory side. Skip retraction; do NOT fall back to raw-content
    judging (that's what CAURA-128 raised the threshold against)."""
    from core_api.services.contradiction_detector import (
        detect_contradictions_by_entities_async,
    )

    new_id, cand_id = uuid4(), uuid4()
    sc = _mock_sc_with_retraction_setup(new_id, cand_id)
    # Override: new side has no links; candidate side keeps its default.
    cand_links = sc.get_entity_links_for_memories.return_value[str(cand_id)]
    sc.get_entity_links_for_memories = AsyncMock(
        return_value={str(new_id): [], str(cand_id): cand_links}
    )
    judge = AsyncMock(return_value=(False, 0.95))

    with (
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_entity_aware_contradiction_check",
            judge,
        ),
        patch(
            "core_api.services.contradiction_detector.resolve_config",
            new_callable=AsyncMock,
            return_value=None,
            create=True,
        ),
    ):
        await detect_contradictions_by_entities_async(new_id, "t1", "f1")

    judge.assert_not_called()
    revert_calls = [
        c
        for c in sc.update_memory_status.call_args_list
        if c.args == (str(cand_id), "active")
    ]
    assert revert_calls == [], (
        "empty entity_links on new memory must skip retraction without "
        "invoking the judge"
    )


@pytest.mark.asyncio
async def test_no_retraction_when_candidate_has_no_entity_links():
    """CAURA-129 — symmetric guard: candidate side has no resolved
    entity_links. The empty-context guard fires regardless of which
    side is degenerate."""
    from core_api.services.contradiction_detector import (
        detect_contradictions_by_entities_async,
    )

    new_id, cand_id = uuid4(), uuid4()
    sc = _mock_sc_with_retraction_setup(new_id, cand_id)
    new_links = sc.get_entity_links_for_memories.return_value[str(new_id)]
    sc.get_entity_links_for_memories = AsyncMock(
        return_value={str(new_id): new_links, str(cand_id): []}
    )
    judge = AsyncMock(return_value=(False, 0.95))

    with (
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_entity_aware_contradiction_check",
            judge,
        ),
        patch(
            "core_api.services.contradiction_detector.resolve_config",
            new_callable=AsyncMock,
            return_value=None,
            create=True,
        ),
    ):
        await detect_contradictions_by_entities_async(new_id, "t1", "f1")

    judge.assert_not_called()
    revert_calls = [
        c
        for c in sc.update_memory_status.call_args_list
        if c.args == (str(cand_id), "active")
    ]
    assert revert_calls == [], (
        "empty entity_links on candidate must skip retraction without "
        "invoking the judge"
    )
