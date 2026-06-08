"""A1 #17 — subject_entity_id preflight gate.

A deterministic gate that fires BEFORE the LLM judge in both
``CheckSemanticDuplicate`` (A1 #16) and Path C
(``detect_contradictions_by_entities_async``, A4 #13).

When the new memory and a candidate both have a non-NULL
``subject_entity_id`` AND those IDs differ, they're definitionally
about different real-world subjects. The judge call is skipped:

  - Dedup gate: skip judge, accept the write
  - Path C gate: skip judge, no contradiction

Why this fixes the Path C first-name-collision follow-up:
The entity extractor links each memory to its OWN entity row even
when several memories share a canonical name like "priya". Two
memories about different Priyas → two distinct ``subject_entity_id``
values → A1 #17's gate fires → no false contradiction.

What it doesn't fix: when the extractor canonicalizes two genuinely
different subjects to one entity row. That requires either better
entity disambiguation (extractor-side) or a prompt that knows about
first-name collisions. A1 #17's gate just plugs the leak where the
data IS available.

Conservative: only the symmetric ``both-non-null AND differ`` case
fires. If either side is NULL (entity extraction hasn't run yet, or
the extractor produced no subject), the gate does nothing — the LLM
judge runs as before.
"""

from __future__ import annotations

import pytest
from tests._contradiction_batch_compat import install_batch_status_replay_shim


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# _subjects_differ_with_certainty — the shared helper.
# ---------------------------------------------------------------------------


def test_helper_returns_true_when_both_present_and_differ():
    """Both rows have ``subject_entity_id`` set; the IDs differ. The
    gate MUST fire (return True so callers know to skip the judge)."""
    from core_api.services.subject_preflight import _subjects_differ_with_certainty

    a = "11111111-1111-1111-1111-111111111111"
    b = "22222222-2222-2222-2222-222222222222"
    assert _subjects_differ_with_certainty(a, b) is True


def test_helper_returns_false_when_both_same():
    """Same ``subject_entity_id`` → could be a genuine same-subject
    pair → don't skip the judge."""
    from core_api.services.subject_preflight import _subjects_differ_with_certainty

    sid = "11111111-1111-1111-1111-111111111111"
    assert _subjects_differ_with_certainty(sid, sid) is False


def test_helper_returns_false_when_left_is_none():
    """One side missing → can't make the determination."""
    from core_api.services.subject_preflight import _subjects_differ_with_certainty

    assert (
        _subjects_differ_with_certainty(None, "11111111-1111-1111-1111-111111111111")
        is False
    )


def test_helper_returns_false_when_right_is_none():
    from core_api.services.subject_preflight import _subjects_differ_with_certainty

    assert (
        _subjects_differ_with_certainty("11111111-1111-1111-1111-111111111111", None)
        is False
    )


def test_helper_returns_false_when_both_none():
    """Both missing (write-time, before any entity extraction) → don't
    skip; fall through to the judge."""
    from core_api.services.subject_preflight import _subjects_differ_with_certainty

    assert _subjects_differ_with_certainty(None, None) is False


def test_helper_accepts_uuid_objects_too():
    """Callers might pass UUID instances rather than strings; the
    helper compares by string form so both shapes work."""
    from uuid import UUID

    from core_api.services.subject_preflight import _subjects_differ_with_certainty

    a = UUID("11111111-1111-1111-1111-111111111111")
    b = UUID("22222222-2222-2222-2222-222222222222")
    assert _subjects_differ_with_certainty(a, b) is True
    assert _subjects_differ_with_certainty(a, a) is False
    # Cross-shape: str + UUID with same value still match.
    assert _subjects_differ_with_certainty(str(a), a) is False


# ---------------------------------------------------------------------------
# Dedup gate — CheckSemanticDuplicate skips judge when subjects differ.
# ---------------------------------------------------------------------------


def _build_ctx(*, subject_entity_id=None):
    from unittest.mock import MagicMock

    ctx = MagicMock()
    ctx.tenant_config = MagicMock(semantic_dedup_enabled=True)
    ctx.data = {
        "input": MagicMock(
            tenant_id="t1",
            fleet_id="f1",
            visibility="scope_team",
            subject_entity_id=subject_entity_id,
            content="new statement",
        ),
        "embedding": [0.1] * 10,
        "memory_fields": {"metadata": {}},
    }
    return ctx


@pytest.mark.asyncio
async def test_dedup_skips_judge_when_subjects_differ():
    """JUDGE-band candidate but distinct subject IDs → A1 #17 skips
    the LLM call. Write is accepted; no 409."""
    from unittest.mock import AsyncMock, patch

    from core_api.pipeline.steps.write.check_semantic_duplicate import (
        CheckSemanticDuplicate,
    )

    new_subject = "11111111-1111-1111-1111-111111111111"
    cand_subject = "22222222-2222-2222-2222-222222222222"
    ctx = _build_ctx(subject_entity_id=new_subject)
    candidate = {
        "id": "00000000-0000-0000-0000-000000000001",
        "similarity": 0.91,
        "content": "candidate statement",
        "subject_entity_id": cand_subject,
    }

    judge = AsyncMock()
    with (
        patch(
            "core_api.pipeline.steps.write.check_semantic_duplicate._find_semantic_duplicate",
            new=AsyncMock(return_value=candidate),
        ),
        patch(
            "core_api.pipeline.steps.write.check_semantic_duplicate._llm_dedup_check",
            new=judge,
        ),
    ):
        step = CheckSemanticDuplicate()
        result = await step.execute(ctx)

    assert result is None  # accepted
    judge.assert_not_called()


@pytest.mark.asyncio
async def test_dedup_calls_judge_when_subjects_match():
    """Same ``subject_entity_id`` → fall through to the existing
    A1 #16 judge dispatch."""
    from unittest.mock import AsyncMock, patch

    from core_api.pipeline.steps.write.check_semantic_duplicate import (
        CheckSemanticDuplicate,
    )

    same = "11111111-1111-1111-1111-111111111111"
    ctx = _build_ctx(subject_entity_id=same)
    candidate = {
        "id": "00000000-0000-0000-0000-000000000002",
        "similarity": 0.91,
        "content": "candidate statement",
        "subject_entity_id": same,
    }

    judge = AsyncMock(return_value=(False, 0.90))
    with (
        patch(
            "core_api.pipeline.steps.write.check_semantic_duplicate._find_semantic_duplicate",
            new=AsyncMock(return_value=candidate),
        ),
        patch(
            "core_api.pipeline.steps.write.check_semantic_duplicate._llm_dedup_check",
            new=judge,
        ),
    ):
        step = CheckSemanticDuplicate()
        await step.execute(ctx)

    judge.assert_called_once()


@pytest.mark.asyncio
async def test_dedup_calls_judge_when_one_side_missing_subject():
    """Either side NULL → can't make a deterministic call; fall
    through to the judge. This is the common case at write-time
    when entity extraction hasn't populated subject_entity_id yet."""
    from unittest.mock import AsyncMock, patch

    from core_api.pipeline.steps.write.check_semantic_duplicate import (
        CheckSemanticDuplicate,
    )

    ctx = _build_ctx(subject_entity_id=None)  # new side has no subject
    candidate = {
        "id": "00000000-0000-0000-0000-000000000003",
        "similarity": 0.91,
        "content": "candidate statement",
        "subject_entity_id": "11111111-1111-1111-1111-111111111111",
    }

    judge = AsyncMock(return_value=(False, 0.90))
    with (
        patch(
            "core_api.pipeline.steps.write.check_semantic_duplicate._find_semantic_duplicate",
            new=AsyncMock(return_value=candidate),
        ),
        patch(
            "core_api.pipeline.steps.write.check_semantic_duplicate._llm_dedup_check",
            new=judge,
        ),
    ):
        step = CheckSemanticDuplicate()
        await step.execute(ctx)

    judge.assert_called_once()


@pytest.mark.asyncio
async def test_dedup_auto_band_still_rejects_regardless_of_subject():
    """Subjects differ AND similarity ≥ AUTO (0.97): A1 #17's gate is
    PRE-judge but auto-reject is an even-higher-confidence call (exact
    or near-exact text). Don't override it — keep the 409.

    Rationale: similarity ≥ 0.97 means the texts are near-identical at
    the embedding level. If the entity extractor produced different
    subject IDs for near-identical text, the extractor is more likely
    wrong than the embedding. Don't second-guess the auto band."""
    from unittest.mock import AsyncMock, patch

    from fastapi import HTTPException

    from core_api.pipeline.steps.write.check_semantic_duplicate import (
        CheckSemanticDuplicate,
    )

    ctx = _build_ctx(subject_entity_id="11111111-1111-1111-1111-111111111111")
    candidate = {
        "id": "00000000-0000-0000-0000-000000000004",
        "similarity": 0.98,  # auto band
        "content": "near-identical content",
        "subject_entity_id": "22222222-2222-2222-2222-222222222222",
    }

    judge = AsyncMock()
    with (
        patch(
            "core_api.pipeline.steps.write.check_semantic_duplicate._find_semantic_duplicate",
            new=AsyncMock(return_value=candidate),
        ),
        patch(
            "core_api.pipeline.steps.write.check_semantic_duplicate._llm_dedup_check",
            new=judge,
        ),
    ):
        step = CheckSemanticDuplicate()
        with pytest.raises(HTTPException) as ei:
            await step.execute(ctx)

    assert ei.value.status_code == 409
    judge.assert_not_called()  # auto band → no LLM


# ---------------------------------------------------------------------------
# Path C gate — detect_contradictions_by_entities_async filters candidates
# with distinct subject_entity_ids before the LLM batch.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_path_c_filters_candidates_with_distinct_subject():
    """The entity-overlap query returns memories sharing a canonical
    name like "priya"; A1 #17 filters out those whose subject_entity_id
    differs from the new memory's BEFORE building the LLM batch.
    Closes the Path C first-name-collision follow-up from the A4 #13
    wet test."""
    from unittest.mock import AsyncMock, patch
    from uuid import uuid4

    from core_api.services.contradiction_detector import (
        detect_contradictions_by_entities_async,
    )

    mid = uuid4()
    new_subject = "11111111-1111-1111-1111-111111111111"
    candidate_subject = "22222222-2222-2222-2222-222222222222"

    new_memory = {
        "id": str(mid),
        "tenant_id": "t1",
        "fleet_id": "f1",
        "content": "Priya joined the Berlin office",
        "subject_entity_id": new_subject,
        "supersedes_id": None,
        "deleted_at": None,
        "status": "active",
        "visibility": "scope_team",
    }
    distinct_subject_candidate = {
        "id": str(uuid4()),
        "content": "Priya left for Bangalore",
        "subject_entity_id": candidate_subject,  # different Priya
        "status": "active",
        "created_at": "2026-05-20T10:00:00+00:00",
    }

    sc = AsyncMock()
    sc.get_memory = AsyncMock(return_value=new_memory)
    sc.find_entity_overlap_candidates = AsyncMock(
        return_value=[distinct_subject_candidate]
    )
    sc.update_memory_status = AsyncMock()
    install_batch_status_replay_shim(sc)

    judge = AsyncMock()
    with (
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_contradiction_check",
            new=judge,
        ),
        patch(
            "core_api.services.contradiction_detector._acquire_path_c_lock",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "core_api.services.contradiction_detector._attempt_path_c_retraction",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=None),
        ),
    ):
        await detect_contradictions_by_entities_async(mid, "t1", "f1")

    # Judge skipped because subjects differ.
    judge.assert_not_called()
    # No retraction / contradiction writes.
    for c in sc.update_memory_status.call_args_list:
        assert "conflicted" not in c.args, f"unexpected contradiction write: {c}"


@pytest.mark.asyncio
async def test_path_c_calls_judge_when_subjects_match():
    """Candidate with matching ``subject_entity_id`` → the preflight
    does not fire; LLM judge runs (existing A4 #13 behaviour)."""
    from unittest.mock import AsyncMock, patch
    from uuid import uuid4

    from core_api.services.contradiction_detector import (
        detect_contradictions_by_entities_async,
    )

    mid = uuid4()
    shared_subject = "11111111-1111-1111-1111-111111111111"

    new_memory = {
        "id": str(mid),
        "tenant_id": "t1",
        "fleet_id": "f1",
        "content": "Yuki Tanaka is the CTO",
        "subject_entity_id": shared_subject,
        "supersedes_id": None,
        "deleted_at": None,
        "status": "active",
        "visibility": "scope_team",
    }
    matching_subject_candidate = {
        "id": str(uuid4()),
        "content": "Yuki Tanaka left the company",
        "subject_entity_id": shared_subject,  # same Yuki
        "status": "active",
        "created_at": "2026-05-20T10:00:00+00:00",
    }

    sc = AsyncMock()
    sc.get_memory = AsyncMock(return_value=new_memory)
    sc.find_entity_overlap_candidates = AsyncMock(
        return_value=[matching_subject_candidate]
    )
    sc.update_memory_status = AsyncMock()
    install_batch_status_replay_shim(sc)

    judge = AsyncMock(return_value=(False, 0.90))
    with (
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_contradiction_check",
            new=judge,
        ),
        patch(
            "core_api.services.contradiction_detector._acquire_path_c_lock",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "core_api.services.contradiction_detector._attempt_path_c_retraction",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=None),
        ),
    ):
        await detect_contradictions_by_entities_async(mid, "t1", "f1")

    judge.assert_called_once()


@pytest.mark.asyncio
async def test_path_c_falls_through_when_new_memory_has_no_subject():
    """``new_memory.subject_entity_id`` is NULL (e.g. enrichment didn't
    set it) → preflight does NOT filter anything; LLM judge runs on
    every candidate."""
    from unittest.mock import AsyncMock, patch
    from uuid import uuid4

    from core_api.services.contradiction_detector import (
        detect_contradictions_by_entities_async,
    )

    mid = uuid4()
    new_memory = {
        "id": str(mid),
        "tenant_id": "t1",
        "fleet_id": "f1",
        "content": "some statement",
        "subject_entity_id": None,  # no subject → can't preflight
        "supersedes_id": None,
        "deleted_at": None,
        "status": "active",
        "visibility": "scope_team",
    }
    candidate = {
        "id": str(uuid4()),
        "content": "candidate statement",
        "subject_entity_id": "11111111-1111-1111-1111-111111111111",
        "status": "active",
        "created_at": "2026-05-20T10:00:00+00:00",
    }

    sc = AsyncMock()
    sc.get_memory = AsyncMock(return_value=new_memory)
    sc.find_entity_overlap_candidates = AsyncMock(return_value=[candidate])
    sc.update_memory_status = AsyncMock()
    install_batch_status_replay_shim(sc)

    judge = AsyncMock(return_value=(False, 0.90))
    with (
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_contradiction_check",
            new=judge,
        ),
        patch(
            "core_api.services.contradiction_detector._acquire_path_c_lock",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "core_api.services.contradiction_detector._attempt_path_c_retraction",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=None),
        ),
    ):
        await detect_contradictions_by_entities_async(mid, "t1", "f1")

    judge.assert_called_once()
