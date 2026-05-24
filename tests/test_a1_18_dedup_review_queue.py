"""A1 #18 — dedup review queue.

Backend-only minimal queue for ambiguous dedup decisions surfaced by
``CheckSemanticDuplicate``. Three enqueue paths:

  - ``auto_reject``           — sim ≥ AUTO; write 409'd
  - ``judge_band_reject``     — judge says high-conf duplicate; write 409'd
  - ``judge_low_conf_accept`` — judge says low-conf duplicate; write accepted

This PR provides:
  - The ``dedup_reviews`` table + ``DedupReview`` model (migration 018)
  - Storage service methods + HTTP routes (list / decide / enqueue)
  - Storage-client helpers
  - The enqueue hooks in ``CheckSemanticDuplicate``

Dashboard UI is out of scope (separate enterprise-repo PR).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest


# ---------------------------------------------------------------------------
# Storage layer — enqueue + list + decide
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_enqueue_persists_row(sc):
    """``enqueue_dedup_review`` inserts a row in ``pending`` status with
    the supplied snapshot fields."""
    cand_id = str(uuid4())
    payload = {
        "tenant_id": "test-tenant-a1-18-1",
        "fleet_id": "fleet-1",
        "agent_id": "agent-1",
        "new_memory_id": None,  # rejected write — no row was persisted
        "candidate_memory_id": cand_id,
        "new_content": "Sigrun joined the Reykjavik office",
        "candidate_content": "Sigrun joined the puffin sanctuary in Reykjavik",
        "similarity": 0.98,
        "judge_verdict": None,
        "judge_confidence": None,
        "decision_band": "auto_reject",
    }
    review = await sc.enqueue_dedup_review(payload)
    assert review["id"]
    assert review["status"] == "pending"
    assert review["decision_band"] == "auto_reject"
    assert review["similarity"] == pytest.approx(0.98)
    assert review["new_memory_id"] is None


@pytest.mark.integration
async def test_list_returns_pending_only_by_default(sc):
    """``list_dedup_reviews`` defaults to ``status='pending'`` so a
    busy queue doesn't drown the caller in already-decided rows."""
    tenant = f"test-tenant-a1-18-list-{uuid4().hex[:8]}"
    cand_id = str(uuid4())

    pending = await sc.enqueue_dedup_review(
        {
            "tenant_id": tenant,
            "fleet_id": "f",
            "agent_id": "a",
            "new_memory_id": None,
            "candidate_memory_id": cand_id,
            "new_content": "X",
            "candidate_content": "Y",
            "similarity": 0.92,
            "judge_verdict": True,
            "judge_confidence": 0.90,
            "decision_band": "judge_band_reject",
        }
    )
    decided = await sc.enqueue_dedup_review(
        {
            "tenant_id": tenant,
            "fleet_id": "f",
            "agent_id": "a",
            "new_memory_id": None,
            "candidate_memory_id": cand_id,
            "new_content": "X2",
            "candidate_content": "Y2",
            "similarity": 0.99,
            "judge_verdict": None,
            "judge_confidence": None,
            "decision_band": "auto_reject",
        }
    )
    # Mark one as decided.
    await sc.decide_dedup_review(decided["id"], "dismissed", decided_by="reviewer-1")

    rows = await sc.list_dedup_reviews({"tenant_id": tenant})
    ids = {r["id"] for r in rows}
    assert pending["id"] in ids
    assert decided["id"] not in ids


@pytest.mark.integration
async def test_decide_sets_status_and_decided_fields(sc):
    """``decide_dedup_review`` transitions ``pending`` → one of the
    terminal statuses and records who decided and when."""
    tenant = f"test-tenant-a1-18-decide-{uuid4().hex[:8]}"
    cand_id = str(uuid4())
    review = await sc.enqueue_dedup_review(
        {
            "tenant_id": tenant,
            "fleet_id": None,
            "agent_id": "a",
            "new_memory_id": None,
            "candidate_memory_id": cand_id,
            "new_content": "alpha",
            "candidate_content": "beta",
            "similarity": 0.93,
            "judge_verdict": True,
            "judge_confidence": 0.80,
            "decision_band": "judge_band_reject",
        }
    )
    updated = await sc.decide_dedup_review(
        review["id"], "override_not_duplicate", decided_by="reviewer-7"
    )
    assert updated["status"] == "override_not_duplicate"
    assert updated["decided_by"] == "reviewer-7"
    assert updated["decided_at"] is not None


@pytest.mark.integration
async def test_decide_rejects_unknown_status(sc):
    """Unknown ``status`` value → 400. Pins the status enum at the
    HTTP boundary so a typo in the reviewer client can't write garbage."""
    import httpx

    tenant = f"test-tenant-a1-18-bad-{uuid4().hex[:8]}"
    cand_id = str(uuid4())
    review = await sc.enqueue_dedup_review(
        {
            "tenant_id": tenant,
            "fleet_id": None,
            "agent_id": "a",
            "new_memory_id": None,
            "candidate_memory_id": cand_id,
            "new_content": "x",
            "candidate_content": "y",
            "similarity": 0.90,
            "judge_verdict": None,
            "judge_confidence": None,
            "decision_band": "auto_reject",
        }
    )
    with pytest.raises(httpx.HTTPStatusError) as ei:
        await sc.decide_dedup_review(review["id"], "rofl", decided_by="r")
    assert ei.value.response.status_code == 400


# ---------------------------------------------------------------------------
# CheckSemanticDuplicate enqueue hook — all 3 paths fire enqueue.
# ---------------------------------------------------------------------------


def _build_ctx(*, subject_entity_id=None):
    ctx = MagicMock()
    ctx.tenant_config = MagicMock(semantic_dedup_enabled=True)
    ctx.data = {
        "input": MagicMock(
            tenant_id="t1",
            fleet_id="f1",
            agent_id="a1",
            visibility="scope_team",
            subject_entity_id=subject_entity_id,
            content="new memory content",
        ),
        "embedding": [0.1] * 10,
        "memory_fields": {"metadata": {}},
    }
    return ctx


@pytest.mark.unit
@pytest.mark.asyncio
async def test_check_dup_enqueues_on_auto_reject():
    """Auto-band 409 → enqueue with ``decision_band='auto_reject'`` and
    NULL judge fields."""
    from fastapi import HTTPException

    from core_api.pipeline.steps.write.check_semantic_duplicate import (
        CheckSemanticDuplicate,
    )

    ctx = _build_ctx()
    cand_id = str(uuid4())
    candidate = {
        "id": cand_id,
        "similarity": 0.98,
        "content": "candidate content",
        "subject_entity_id": None,
    }
    enqueue = AsyncMock(return_value={"id": str(uuid4())})

    with (
        patch(
            "core_api.pipeline.steps.write.check_semantic_duplicate._find_semantic_duplicate",
            new=AsyncMock(return_value=candidate),
        ),
        patch(
            "core_api.pipeline.steps.write.check_semantic_duplicate._enqueue_dedup_review",
            new=enqueue,
        ),
    ):
        step = CheckSemanticDuplicate()
        with pytest.raises(HTTPException):
            await step.execute(ctx)

    enqueue.assert_called_once()
    kwargs = enqueue.call_args.kwargs
    assert kwargs.get("decision_band") == "auto_reject"
    assert kwargs.get("judge_verdict") is None
    assert kwargs.get("judge_confidence") is None
    assert kwargs.get("candidate_memory_id") == cand_id
    assert kwargs.get("new_memory_id") is None
    assert kwargs.get("similarity") == pytest.approx(0.98)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_check_dup_enqueues_on_judge_band_high_conf_reject():
    """Judge band + ``is_duplicate=True`` at conf ≥ threshold → 409 +
    enqueue with ``decision_band='judge_band_reject'`` and the judge's
    confidence."""
    from fastapi import HTTPException

    from core_api.pipeline.steps.write.check_semantic_duplicate import (
        CheckSemanticDuplicate,
    )

    ctx = _build_ctx()
    candidate = {
        "id": str(uuid4()),
        "similarity": 0.91,
        "content": "candidate content",
        "subject_entity_id": None,
    }
    enqueue = AsyncMock(return_value={"id": str(uuid4())})

    with (
        patch(
            "core_api.pipeline.steps.write.check_semantic_duplicate._find_semantic_duplicate",
            new=AsyncMock(return_value=candidate),
        ),
        patch(
            "core_api.pipeline.steps.write.check_semantic_duplicate._llm_dedup_check",
            new=AsyncMock(return_value=(True, 0.90)),
        ),
        patch(
            "core_api.pipeline.steps.write.check_semantic_duplicate._enqueue_dedup_review",
            new=enqueue,
        ),
    ):
        step = CheckSemanticDuplicate()
        with pytest.raises(HTTPException):
            await step.execute(ctx)

    enqueue.assert_called_once()
    kwargs = enqueue.call_args.kwargs
    assert kwargs.get("decision_band") == "judge_band_reject"
    assert kwargs.get("judge_verdict") is True
    assert kwargs.get("judge_confidence") == pytest.approx(0.90)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_check_dup_enqueues_on_judge_band_low_conf_accept():
    """Judge band + ``is_duplicate=True`` at conf < threshold → write
    ACCEPTED + enqueue with ``decision_band='judge_low_conf_accept'``.
    The write succeeded but there was a near-miss worth review."""
    from core_api.pipeline.steps.write.check_semantic_duplicate import (
        CheckSemanticDuplicate,
    )

    ctx = _build_ctx()
    candidate = {
        "id": str(uuid4()),
        "similarity": 0.91,
        "content": "candidate content",
        "subject_entity_id": None,
    }
    enqueue = AsyncMock(return_value={"id": str(uuid4())})

    with (
        patch(
            "core_api.pipeline.steps.write.check_semantic_duplicate._find_semantic_duplicate",
            new=AsyncMock(return_value=candidate),
        ),
        patch(
            "core_api.pipeline.steps.write.check_semantic_duplicate._llm_dedup_check",
            new=AsyncMock(return_value=(True, 0.50)),  # low conf
        ),
        patch(
            "core_api.pipeline.steps.write.check_semantic_duplicate._enqueue_dedup_review",
            new=enqueue,
        ),
    ):
        step = CheckSemanticDuplicate()
        result = await step.execute(ctx)

    assert result is None  # accepted
    enqueue.assert_called_once()
    kwargs = enqueue.call_args.kwargs
    assert kwargs.get("decision_band") == "judge_low_conf_accept"
    assert kwargs.get("judge_verdict") is True
    assert kwargs.get("judge_confidence") == pytest.approx(0.50)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_check_dup_does_not_enqueue_on_judge_band_not_duplicate():
    """Judge says ``is_duplicate=False`` → write accepted, NO queue
    entry. Don't pollute the review surface with confidently-not-a-dup
    cases."""
    from core_api.pipeline.steps.write.check_semantic_duplicate import (
        CheckSemanticDuplicate,
    )

    ctx = _build_ctx()
    candidate = {
        "id": str(uuid4()),
        "similarity": 0.90,
        "content": "candidate content",
        "subject_entity_id": None,
    }
    enqueue = AsyncMock()

    with (
        patch(
            "core_api.pipeline.steps.write.check_semantic_duplicate._find_semantic_duplicate",
            new=AsyncMock(return_value=candidate),
        ),
        patch(
            "core_api.pipeline.steps.write.check_semantic_duplicate._llm_dedup_check",
            new=AsyncMock(return_value=(False, 0.90)),
        ),
        patch(
            "core_api.pipeline.steps.write.check_semantic_duplicate._enqueue_dedup_review",
            new=enqueue,
        ),
    ):
        step = CheckSemanticDuplicate()
        result = await step.execute(ctx)

    assert result is None
    enqueue.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_check_dup_does_not_enqueue_when_preflight_skips():
    """A1 #17 subject preflight skipped the judge (subjects differ).
    Write accepted; no review needed — the deterministic gate ruled
    them out."""
    from core_api.pipeline.steps.write.check_semantic_duplicate import (
        CheckSemanticDuplicate,
    )

    new_subject = "11111111-1111-1111-1111-111111111111"
    cand_subject = "22222222-2222-2222-2222-222222222222"
    ctx = _build_ctx(subject_entity_id=new_subject)
    candidate = {
        "id": str(uuid4()),
        "similarity": 0.91,
        "content": "candidate content",
        "subject_entity_id": cand_subject,
    }
    enqueue = AsyncMock()

    with (
        patch(
            "core_api.pipeline.steps.write.check_semantic_duplicate._find_semantic_duplicate",
            new=AsyncMock(return_value=candidate),
        ),
        patch(
            "core_api.pipeline.steps.write.check_semantic_duplicate._enqueue_dedup_review",
            new=enqueue,
        ),
    ):
        step = CheckSemanticDuplicate()
        await step.execute(ctx)

    enqueue.assert_not_called()
