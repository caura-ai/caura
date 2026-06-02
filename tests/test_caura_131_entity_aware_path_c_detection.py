"""CAURA-131 — Path C entity-overlap detection now uses the entity-aware
LLM judge when resolved entity context is available for both sides.

Mirrors the CAURA-129 retraction-path fix into the FORWARD detection
path. Wet-tested case (priya-from-Acme vs priya-from-Beta, same
canonical entity, different surface qualifiers, different single-value
predicate value):

  * Pre-CAURA-131: base ``_llm_contradiction_check`` saw
    ``subject_a="Priya from AcmeCorp"`` vs
    ``subject_b="Priya from BetaIndustries"`` → ``same_subject=false``
    → no flag. Genuine within-subject contradictions silently dropped.

  * Post-CAURA-131: ``_llm_entity_aware_contradiction_check`` sees the
    resolved entity rows authoritatively (same ``entity_id``) →
    ``same_subject=true`` → flag fires.

Tests below lock in the wiring (entity-aware judge used when contexts
present; base judge used as fallback when contexts empty / fetch
errored / cap exceeded).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from tests._contradiction_batch_compat import install_batch_status_replay_shim

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers (mirror tests/test_caura_130_path_c_safety.py shape)
# ---------------------------------------------------------------------------


def _make_candidate(
    mid, *, subject_entity_id=None, content: str = "candidate content"
) -> dict:
    return {
        "id": str(mid),
        "tenant_id": "t1",
        "fleet_id": "f1",
        "content": content,
        "subject_entity_id": subject_entity_id,
        "visibility": "scope_team",
        "deleted_at": None,
        "created_at": "2026-05-24T10:00:00+00:00",
    }


def _make_new_memory(
    mid, *, subject_entity_id=None, content: str = "new memory content"
) -> dict:
    return {
        "id": str(mid),
        "tenant_id": "t1",
        "fleet_id": "f1",
        "content": content,
        "subject_entity_id": subject_entity_id,
        "visibility": "scope_team",
        "supersedes_id": None,
        "deleted_at": None,
        "created_at": "2026-05-24T11:00:00+00:00",
    }


def _sc(
    new_mem: dict, candidates: list[dict], links_by_mem: dict[str, list[dict]]
) -> AsyncMock:
    sc = AsyncMock()

    async def get_memory(mid: str):
        if mid == new_mem["id"]:
            return new_mem
        for c in candidates:
            if c["id"] == mid:
                return c
        return None

    sc.get_memory = AsyncMock(side_effect=get_memory)
    sc.find_entity_overlap_candidates = AsyncMock(return_value=candidates)
    sc.update_memory_status = AsyncMock()
    sc.get_entity_links_for_memories = AsyncMock(return_value=links_by_mem)

    async def get_entity(eid: str):
        return {
            "id": eid,
            "canonical_name": eid.split(":", 1)[-1] if ":" in eid else eid,
            "entity_type": "person" if "priya" in eid else "project",
        }

    sc.get_entity = AsyncMock(side_effect=get_entity)
    install_batch_status_replay_shim(sc)
    return sc


# ---------------------------------------------------------------------------
# Entity-aware judge fires when both contexts present
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_entity_aware_judge_used_when_both_contexts_present():
    """CAURA-131 — the priya-shape wet-test repro. Same canonical
    subject (same entity_id), opposing single-value predicate
    contents. The entity-aware judge MUST be invoked (base judge
    MUST NOT). Locks in the fix that resolves the wet-test silence."""
    from core_api.services.contradiction_detector import (
        detect_contradictions_by_entities_async,
    )

    new_id, cand_id = uuid4(), uuid4()
    # Both sides resolve to the SAME canonical subject (same entity_id).
    # subject_entity_id NULL on both → A1 #17 passes them through.
    new_mem = _make_new_memory(
        new_id,
        subject_entity_id=None,
        content="Priya from AcmeCorp lives in Tel Aviv.",
    )
    cand = _make_candidate(
        cand_id,
        subject_entity_id=None,
        content="Priya from BetaIndustries lives in Haifa.",
    )
    links = {
        str(new_id): [{"entity_id": "ent:priya", "role": "subject"}],
        str(cand_id): [{"entity_id": "ent:priya", "role": "subject"}],
    }
    sc = _sc(new_mem, [cand], links)

    base_judge = AsyncMock(return_value=(False, 0.95))
    entity_aware_judge = AsyncMock(return_value=(True, 0.95))  # flag the contradiction

    with (
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_contradiction_check",
            base_judge,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_entity_aware_contradiction_check",
            entity_aware_judge,
        ),
        patch(
            "core_api.services.contradiction_detector.resolve_config",
            new_callable=AsyncMock,
            return_value=None,
            create=True,
        ),
        patch(
            "core_api.services.contradiction_detector._acquire_path_c_lock",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        await detect_contradictions_by_entities_async(new_id, "t1", "f1")

    entity_aware_judge.assert_called_once()
    base_judge.assert_not_called()
    # Verify the resolved entities reached the judge (otherwise the
    # promise of "authoritative entity context" isn't kept).
    call = entity_aware_judge.call_args
    new_entities_arg = call.args[2]
    old_entities_arg = call.args[3]
    assert any(e.get("entity_id") == "ent:priya" for e in new_entities_arg)
    assert any(e.get("entity_id") == "ent:priya" for e in old_entities_arg)


# ---------------------------------------------------------------------------
# Fallback paths — base judge runs when contexts are unavailable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_to_base_judge_when_new_memory_has_empty_context():
    """When the entity-extraction worker hasn't populated entity_links
    for the new memory yet, we cannot run the entity-aware judge
    meaningfully. Fall back to the base ``_llm_contradiction_check``
    so detection still runs (just without the entity-aware lift)."""
    from core_api.services.contradiction_detector import (
        detect_contradictions_by_entities_async,
    )

    new_id, cand_id = uuid4(), uuid4()
    new_mem = _make_new_memory(new_id, subject_entity_id=None)
    cand = _make_candidate(cand_id, subject_entity_id=None)
    links = {
        str(new_id): [],  # ← empty on new side
        str(cand_id): [{"entity_id": "ent:priya", "role": "subject"}],
    }
    sc = _sc(new_mem, [cand], links)
    base_judge = AsyncMock(return_value=(False, 0.95))
    entity_aware_judge = AsyncMock(return_value=(True, 0.95))

    with (
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_contradiction_check",
            base_judge,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_entity_aware_contradiction_check",
            entity_aware_judge,
        ),
        patch(
            "core_api.services.contradiction_detector.resolve_config",
            new_callable=AsyncMock,
            return_value=None,
            create=True,
        ),
        patch(
            "core_api.services.contradiction_detector._acquire_path_c_lock",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        await detect_contradictions_by_entities_async(new_id, "t1", "f1")

    # Empty new-side context → base judge runs (not entity-aware).
    base_judge.assert_called_once()
    entity_aware_judge.assert_not_called()


@pytest.mark.asyncio
async def test_fallback_to_base_judge_when_candidate_has_empty_context():
    """Symmetric to the above — candidate side has no entity_links."""
    from core_api.services.contradiction_detector import (
        detect_contradictions_by_entities_async,
    )

    new_id, cand_id = uuid4(), uuid4()
    new_mem = _make_new_memory(new_id, subject_entity_id=None)
    cand = _make_candidate(cand_id, subject_entity_id=None)
    links = {
        str(new_id): [{"entity_id": "ent:priya", "role": "subject"}],
        str(cand_id): [],  # ← empty on candidate side
    }
    sc = _sc(new_mem, [cand], links)
    base_judge = AsyncMock(return_value=(False, 0.95))
    entity_aware_judge = AsyncMock(return_value=(True, 0.95))

    with (
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_contradiction_check",
            base_judge,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_entity_aware_contradiction_check",
            entity_aware_judge,
        ),
        patch(
            "core_api.services.contradiction_detector.resolve_config",
            new_callable=AsyncMock,
            return_value=None,
            create=True,
        ),
        patch(
            "core_api.services.contradiction_detector._acquire_path_c_lock",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        await detect_contradictions_by_entities_async(new_id, "t1", "f1")

    base_judge.assert_called_once()
    entity_aware_judge.assert_not_called()


@pytest.mark.asyncio
async def test_fallback_to_base_judge_when_context_fetch_fails():
    """Storage failure during the entity-context fetch must not
    silently drop candidates — fail open and run the base judge for
    every candidate. Conservative against losing real contradictions
    on a transient storage hiccup."""
    from core_api.services.contradiction_detector import (
        detect_contradictions_by_entities_async,
    )

    new_id, cand_id = uuid4(), uuid4()
    new_mem = _make_new_memory(new_id, subject_entity_id=None)
    cand = _make_candidate(cand_id, subject_entity_id=None)
    sc = _sc(new_mem, [cand], {})
    sc.get_entity_links_for_memories = AsyncMock(
        side_effect=RuntimeError("storage down")
    )

    base_judge = AsyncMock(return_value=(True, 0.95))
    entity_aware_judge = AsyncMock(return_value=(True, 0.95))

    with (
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_contradiction_check",
            base_judge,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_entity_aware_contradiction_check",
            entity_aware_judge,
        ),
        patch(
            "core_api.services.contradiction_detector.resolve_config",
            new_callable=AsyncMock,
            return_value=None,
            create=True,
        ),
        patch(
            "core_api.services.contradiction_detector._acquire_path_c_lock",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        await detect_contradictions_by_entities_async(new_id, "t1", "f1")

    # Fetch failure → entity-aware path bypassed; base judge runs.
    base_judge.assert_called_once()
    entity_aware_judge.assert_not_called()


@pytest.mark.asyncio
async def test_fallback_to_base_judge_when_candidate_set_exceeds_cap():
    """Cost guard — when the candidate set is large enough that
    fetching context for all of them would cost too many round-trips,
    skip the fetch entirely and use the base judge. The L3.4 cap
    bounds the storage fan-out at the cost of losing the entity-
    aware lift for this round (acceptable; popular entities are rare
    and the base judge still flags some real contradictions)."""
    from core_api.services.contradiction_detector import (
        _ENTITY_LINKS_PREFLIGHT_MAX_CANDIDATES,
        detect_contradictions_by_entities_async,
    )

    new_id = uuid4()
    new_mem = _make_new_memory(new_id, subject_entity_id=None)
    cand_ids = [uuid4() for _ in range(_ENTITY_LINKS_PREFLIGHT_MAX_CANDIDATES + 3)]
    cands = [_make_candidate(cid, subject_entity_id=None) for cid in cand_ids]
    sc = _sc(new_mem, cands, {})

    base_judge = AsyncMock(return_value=(False, 0.95))
    entity_aware_judge = AsyncMock(return_value=(True, 0.95))

    with (
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_contradiction_check",
            base_judge,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_entity_aware_contradiction_check",
            entity_aware_judge,
        ),
        patch(
            "core_api.services.contradiction_detector.resolve_config",
            new_callable=AsyncMock,
            return_value=None,
            create=True,
        ),
        patch(
            "core_api.services.contradiction_detector._acquire_path_c_lock",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        await detect_contradictions_by_entities_async(new_id, "t1", "f1")

    # Above the cap: fetch must not run; base judge runs N times.
    sc.get_entity_links_for_memories.assert_not_called()
    assert base_judge.call_count == len(cands)
    entity_aware_judge.assert_not_called()


@pytest.mark.asyncio
async def test_a1_17_matched_candidates_do_not_count_toward_cap():
    """Regression guard — the cap counts FALL-THROUGH candidates only
    (new_subject is NULL or candidate.subject_entity_id is NULL), not
    A1-#17-matched candidates (both sides non-NULL, same entity_id).

    Pre-fix: cap was on ``len(candidates)`` so a memory with a few
    fall-through candidates plus many A1-#17 matches would silently
    skip the fetch and lose entity-aware judging on EVERY candidate,
    including the fall-through ones that genuinely needed L3.4.

    Post-fix: many A1-#17-matched + few fall-through stays under the
    cap; fetch runs; entity-aware judge runs on all candidates."""
    from core_api.services.contradiction_detector import (
        _ENTITY_LINKS_PREFLIGHT_MAX_CANDIDATES,
        detect_contradictions_by_entities_async,
    )

    new_id = uuid4()
    same_sid = "sid-shared"  # both sides will carry this non-NULL id
    new_mem = _make_new_memory(new_id, subject_entity_id=same_sid)
    # Many A1-#17-matched + few fall-through. Sized so:
    #   fallthrough_count (3) ≤ _ENTITY_LINKS_PREFLIGHT_MAX_CANDIDATES (20)
    #   total (23)          ≤ _ENTITY_LINKS_DETECTION_FETCH_MAX_CANDIDATES (40)
    # Both caps OK → fetch runs.
    n_matched = _ENTITY_LINKS_PREFLIGHT_MAX_CANDIDATES  # 20
    n_fallthrough = 3
    matched_cands = [
        _make_candidate(uuid4(), subject_entity_id=same_sid) for _ in range(n_matched)
    ]
    fallthrough_cands = [
        _make_candidate(uuid4(), subject_entity_id=None) for _ in range(n_fallthrough)
    ]
    cands = matched_cands + fallthrough_cands
    # Populate entity_links so the entity-aware judge has context to
    # work with for every candidate.
    links = {str(new_id): [{"entity_id": "ent:shared", "role": "subject"}]}
    for c in cands:
        links[c["id"]] = [{"entity_id": "ent:shared", "role": "subject"}]
    sc = _sc(new_mem, cands, links)

    base_judge = AsyncMock(return_value=(False, 0.95))
    entity_aware_judge = AsyncMock(return_value=(False, 0.95))

    with (
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_contradiction_check",
            base_judge,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_entity_aware_contradiction_check",
            entity_aware_judge,
        ),
        patch(
            "core_api.services.contradiction_detector.resolve_config",
            new_callable=AsyncMock,
            return_value=None,
            create=True,
        ),
        patch(
            "core_api.services.contradiction_detector._acquire_path_c_lock",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        await detect_contradictions_by_entities_async(new_id, "t1", "f1")

    # Fall-through count (3) ≤ preflight cap (20) AND total (23) ≤
    # detection cap (40) → fetch runs. ``_fetch_entity_context``
    # issues one ``get_entity_links_for_memories([single_id])`` call
    # per memory in the parallel gather, so we expect N+1 calls
    # (1 for new_mem + N for candidates), not 1. The bound this
    # test cares about is "fetch DID run" (cap didn't fire).
    assert sc.get_entity_links_for_memories.call_count == len(cands) + 1, (
        f"expected {len(cands) + 1} fetch calls (1 new_mem + {len(cands)} cands); "
        f"got {sc.get_entity_links_for_memories.call_count}"
    )
    # All candidates have non-empty entity context → entity-aware
    # judge runs on every one. Base judge never runs.
    assert entity_aware_judge.call_count == len(cands)
    base_judge.assert_not_called()


@pytest.mark.asyncio
async def test_detection_fetch_skipped_when_total_exceeds_detection_cap():
    """CAURA-131 follow-up — the L3.4 preflight cap on fall-through
    candidates is NOT enough on its own. A popular entity with many
    A1-#17-matched candidates would otherwise issue an unbounded
    parallel fetch (fall-through tiny, total huge). The detection-fetch
    cap (``_ENTITY_LINKS_DETECTION_FETCH_MAX_CANDIDATES``) bounds the
    TOTAL gather size as a thundering-herd guard. Above that bound,
    skip the fetch entirely and fall back to the base judge for all
    candidates."""
    from core_api.services.contradiction_detector import (
        _ENTITY_LINKS_DETECTION_FETCH_MAX_CANDIDATES,
        _ENTITY_LINKS_PREFLIGHT_MAX_CANDIDATES,
        detect_contradictions_by_entities_async,
    )

    new_id = uuid4()
    same_sid = "sid-shared"
    new_mem = _make_new_memory(new_id, subject_entity_id=same_sid)
    # Fall-through (2) well below the preflight cap (20); total
    # candidates above the detection-fetch cap (40). Verifies the
    # SECOND guard (total cap) fires when the FIRST guard
    # (fall-through cap) would not.
    n_fallthrough = 2
    n_matched = _ENTITY_LINKS_DETECTION_FETCH_MAX_CANDIDATES  # 40
    fallthrough_cands = [
        _make_candidate(uuid4(), subject_entity_id=None) for _ in range(n_fallthrough)
    ]
    matched_cands = [
        _make_candidate(uuid4(), subject_entity_id=same_sid) for _ in range(n_matched)
    ]
    cands = fallthrough_cands + matched_cands
    # Sanity: scenario must actually exercise the SECOND guard, not
    # the FIRST — fall-through must be under its cap, total over its.
    assert n_fallthrough <= _ENTITY_LINKS_PREFLIGHT_MAX_CANDIDATES
    assert len(cands) > _ENTITY_LINKS_DETECTION_FETCH_MAX_CANDIDATES

    sc = _sc(new_mem, cands, {})
    base_judge = AsyncMock(return_value=(False, 0.95))
    entity_aware_judge = AsyncMock(return_value=(False, 0.95))

    with (
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_contradiction_check",
            base_judge,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_entity_aware_contradiction_check",
            entity_aware_judge,
        ),
        patch(
            "core_api.services.contradiction_detector.resolve_config",
            new_callable=AsyncMock,
            return_value=None,
            create=True,
        ),
        patch(
            "core_api.services.contradiction_detector._acquire_path_c_lock",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        await detect_contradictions_by_entities_async(new_id, "t1", "f1")

    # Total over cap → fetch must not run.
    sc.get_entity_links_for_memories.assert_not_called()
    # Fall-back: base judge runs on every candidate; entity-aware
    # never invoked.
    assert base_judge.call_count == len(cands)
    entity_aware_judge.assert_not_called()


# ---------------------------------------------------------------------------
# L3.4 preflight still drops collisions (regression guard for CAURA-130)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_l34_preflight_still_drops_collision_after_refactor():
    """The CAURA-130 L3.4 collision drop must keep working under the
    CAURA-131 refactor (shared context fetch). Two memories with
    SAME canonical name but DISTINCT entity_ids → drop. The
    entity-aware judge must NOT be invoked because the candidate was
    filtered out before the LLM call."""
    from core_api.services.contradiction_detector import (
        detect_contradictions_by_entities_async,
    )

    new_id, cand_id = uuid4(), uuid4()
    new_mem = _make_new_memory(new_id, subject_entity_id=None)
    cand = _make_candidate(cand_id, subject_entity_id=None)
    # Distinct entity_ids → preflight drops.
    links = {
        str(new_id): [{"entity_id": "ent:priya-A", "role": "subject"}],
        str(cand_id): [{"entity_id": "ent:priya-B", "role": "subject"}],
    }
    sc = _sc(new_mem, [cand], links)
    base_judge = AsyncMock(return_value=(True, 0.95))
    entity_aware_judge = AsyncMock(return_value=(True, 0.95))

    with (
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_contradiction_check",
            base_judge,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_entity_aware_contradiction_check",
            entity_aware_judge,
        ),
        patch(
            "core_api.services.contradiction_detector.resolve_config",
            new_callable=AsyncMock,
            return_value=None,
            create=True,
        ),
        patch(
            "core_api.services.contradiction_detector._acquire_path_c_lock",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        await detect_contradictions_by_entities_async(new_id, "t1", "f1")

    base_judge.assert_not_called()
    entity_aware_judge.assert_not_called()
