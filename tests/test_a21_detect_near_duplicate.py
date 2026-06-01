"""A21 — ``DetectNearDuplicate`` is the advisory twin of
``CheckSemanticDuplicate`` for the fast / non-strong write path.

Strong mode (unchanged): runs ``CheckSemanticDuplicate`` which 409s when
content lands in the AUTO band or the LLM judge confirms a JUDGE-band
duplicate.

Fast / auto modes (the gap A21 closes): previously skipped semantic
dedup entirely — identical-fact fast writes accumulated independent
rows with no signal to the caller. ``DetectNearDuplicate`` runs ONLY
the AUTO band (no LLM judge, no judge band) and is purely advisory:

  - Never raises.  The write always proceeds to 201.
  - On a high-similarity hit, stashes ``near_duplicate_of`` (uuid str)
    and ``near_duplicate_similarity`` (float, 4dp) in the in-flight
    memory's metadata so the caller / downstream can observe it.
  - Always sets ``near_dup_check_ms`` when the check ran (mirrors the
    strong side's ``semantic_dedup_ms``).
  - Skip gates mirror the strong side: tenant config off, embedding
    missing, identifier-bearing content. Identifier skip ALSO stashes
    a ``near_dup_skipped_reason`` slug.

The step sits in ``build_fast_write_pipeline()`` between
``CheckExactDuplicate`` and ``WriteMemoryRow``. It is NOT in the
strong pipeline (strong already has ``CheckSemanticDuplicate``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Test context helper — mirrors test_a1_identifier_prefilter._build_ctx and
# test_a1_16_dedup_judge_dispatch._build_ctx.
# ---------------------------------------------------------------------------


_DEFAULT_EMBEDDING: list[float] = [0.1] * 10
_UNSET = object()


def _build_ctx(
    *,
    content: str = "Plain natural text that won't trip identifier filters.",
    dedup_enabled: bool = True,
    embedding=_UNSET,
):
    """Minimal fake PipelineContext that exercises the step's actual code
    path. ``embedding`` defaults to a non-None vector — pass ``None``
    explicitly to drive the missing-embedding skip path."""
    if embedding is _UNSET:
        embedding = _DEFAULT_EMBEDDING
    ctx = MagicMock()
    ctx.tenant_config = MagicMock(semantic_dedup_enabled=dedup_enabled)
    ctx.data = {
        "input": MagicMock(
            tenant_id="t1",
            fleet_id="f1",
            agent_id="a1",
            visibility="scope_team",
            subject_entity_id=None,
            content=content,
        ),
        "embedding": embedding,
        "memory_fields": {"metadata": {}},
    }
    return ctx


# ---------------------------------------------------------------------------
# 1. Happy detection — candidate above AUTO threshold → metadata stashed,
#    no exception, step returns None.
# ---------------------------------------------------------------------------


async def test_happy_detection_stashes_near_duplicate_metadata():
    from core_api.pipeline.steps.write.detect_near_duplicate import (
        DetectNearDuplicate,
    )

    ctx = _build_ctx()
    candidate_id = str(uuid4())
    candidate = {
        "id": candidate_id,
        "similarity": 0.97,
        "content": "Existing memory we're advisory-matching against.",
        "subject_entity_id": None,
    }

    with patch(
        "core_api.pipeline.steps.write.detect_near_duplicate._find_semantic_duplicate",
        new=AsyncMock(return_value=candidate),
    ):
        step = DetectNearDuplicate()
        result = await step.execute(ctx)

    # Advisory only — no exception was raised; step returns None on
    # accept-with-stash (mirrors CheckSemanticDuplicate's None-on-accept
    # contract from A1 #16).
    assert result is None

    md = ctx.data["memory_fields"]["metadata"]
    assert md["near_duplicate_of"] == candidate_id
    assert md["near_duplicate_similarity"] == pytest.approx(0.97)
    assert "near_dup_check_ms" in md
    assert isinstance(md["near_dup_check_ms"], float)


# ---------------------------------------------------------------------------
# 2. No candidate — _find_semantic_duplicate returns None.
# ---------------------------------------------------------------------------


async def test_no_candidate_records_only_check_ms():
    from core_api.pipeline.steps.write.detect_near_duplicate import (
        DetectNearDuplicate,
    )

    ctx = _build_ctx()
    with patch(
        "core_api.pipeline.steps.write.detect_near_duplicate._find_semantic_duplicate",
        new=AsyncMock(return_value=None),
    ):
        step = DetectNearDuplicate()
        result = await step.execute(ctx)

    assert result is None
    md = ctx.data["memory_fields"]["metadata"]
    # The check ran, just no candidate landed in the AUTO band.
    assert "near_dup_check_ms" in md
    # And NO advisory stash — the field is the contract signal for
    # "did we find one?", so absence must be load-bearing.
    assert "near_duplicate_of" not in md
    assert "near_duplicate_similarity" not in md


# ---------------------------------------------------------------------------
# 3. Disabled by tenant config — short-circuit, no storage call.
# ---------------------------------------------------------------------------


async def test_disabled_by_tenant_config_returns_skipped():
    from core_api.pipeline.step import StepOutcome
    from core_api.pipeline.steps.write.detect_near_duplicate import (
        DetectNearDuplicate,
    )

    ctx = _build_ctx(dedup_enabled=False)
    find = AsyncMock()
    with patch(
        "core_api.pipeline.steps.write.detect_near_duplicate._find_semantic_duplicate",
        new=find,
    ):
        step = DetectNearDuplicate()
        result = await step.execute(ctx)

    assert result is not None
    assert result.outcome == StepOutcome.SKIPPED
    find.assert_not_called()
    # No check ran → no timing key, no stash.
    md = ctx.data["memory_fields"]["metadata"]
    assert "near_dup_check_ms" not in md
    assert "near_duplicate_of" not in md


# ---------------------------------------------------------------------------
# 4. Disabled by missing embedding — fast-mode-with-deferred-embed path.
# ---------------------------------------------------------------------------


async def test_missing_embedding_returns_skipped():
    from core_api.pipeline.step import StepOutcome
    from core_api.pipeline.steps.write.detect_near_duplicate import (
        DetectNearDuplicate,
    )

    ctx = _build_ctx(embedding=None)
    find = AsyncMock()
    with patch(
        "core_api.pipeline.steps.write.detect_near_duplicate._find_semantic_duplicate",
        new=find,
    ):
        step = DetectNearDuplicate()
        result = await step.execute(ctx)

    assert result is not None
    assert result.outcome == StepOutcome.SKIPPED
    find.assert_not_called()
    md = ctx.data["memory_fields"]["metadata"]
    assert "near_dup_check_ms" not in md


# ---------------------------------------------------------------------------
# 5. Identifier-bearing content — skip with reason slug stash. Mirrors
#    A1's identifier pre-filter behaviour on the strong-side step.
# ---------------------------------------------------------------------------


async def test_identifier_bearing_content_skips_with_reason():
    from core_api.pipeline.step import StepOutcome
    from core_api.pipeline.steps.write.detect_near_duplicate import (
        DetectNearDuplicate,
    )

    ctx = _build_ctx(content="Build pr-456 deployed to staging.")
    find = AsyncMock()
    with (
        patch(
            "core_api.pipeline.steps.write.detect_near_duplicate._content_is_identifier_bearing",
            return_value=True,
        ),
        patch(
            "core_api.pipeline.steps.write.detect_near_duplicate._find_semantic_duplicate",
            new=find,
        ),
    ):
        step = DetectNearDuplicate()
        result = await step.execute(ctx)

    assert result is not None
    assert result.outcome == StepOutcome.SKIPPED
    # Reason exposed both on the step result detail AND in metadata —
    # the metadata stash is the durable signal (lands on the row); the
    # detail is the pipeline-log signal.
    assert result.detail == {"reason": "identifier_prefilter"}
    md = ctx.data["memory_fields"]["metadata"]
    assert md["near_dup_skipped_reason"] == "identifier_prefilter"
    find.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Defensive guard: candidate dict has ``id=None`` (storage corner case).
# ---------------------------------------------------------------------------


async def test_candidate_with_no_id_is_silently_dropped():
    from core_api.pipeline.steps.write.detect_near_duplicate import (
        DetectNearDuplicate,
    )

    ctx = _build_ctx()
    candidate = {
        "id": None,
        "similarity": 0.98,
        "content": "Existing memory but with broken id.",
        "subject_entity_id": None,
    }
    with patch(
        "core_api.pipeline.steps.write.detect_near_duplicate._find_semantic_duplicate",
        new=AsyncMock(return_value=candidate),
    ):
        step = DetectNearDuplicate()
        result = await step.execute(ctx)

    assert result is None
    md = ctx.data["memory_fields"]["metadata"]
    # The check ran — timing always recorded when we reached the call.
    assert "near_dup_check_ms" in md
    # But the defensive guard suppressed the stash (no useful UUID to
    # surface to the caller).
    assert "near_duplicate_of" not in md
    assert "near_duplicate_similarity" not in md


# ---------------------------------------------------------------------------
# 7. JUDGE threshold is the one passed to _find_semantic_duplicate — wider
#    than AUTO so the advisory step catches real paraphrases that sit in
#    the 0.85–0.97 band, without the LLM judge call (advisory, no reject).
# ---------------------------------------------------------------------------


async def test_calls_find_with_judge_threshold():
    from common.constants import (
        SEMANTIC_DEDUP_AUTO_THRESHOLD,
        SEMANTIC_DEDUP_JUDGE_THRESHOLD,
    )
    from core_api.pipeline.steps.write.detect_near_duplicate import (
        DetectNearDuplicate,
    )

    ctx = _build_ctx()
    find = AsyncMock(return_value=None)
    with patch(
        "core_api.pipeline.steps.write.detect_near_duplicate._find_semantic_duplicate",
        new=find,
    ):
        step = DetectNearDuplicate()
        await step.execute(ctx)

    find.assert_called_once()
    # ``_find_semantic_duplicate`` is called with ``min_similarity`` as a
    # keyword arg (positional shape may shift; the contract is the
    # keyword). Cross-check both shapes to stay robust to a minor refactor.
    call_kwargs = find.call_args.kwargs
    if "min_similarity" in call_kwargs:
        threshold = call_kwargs["min_similarity"]
    else:
        # Positional fallback — find the float-in-(0,1) arg.
        threshold = next(
            a for a in find.call_args.args if isinstance(a, float) and 0.0 < a < 1.0
        )
    assert threshold == SEMANTIC_DEDUP_JUDGE_THRESHOLD
    assert threshold != SEMANTIC_DEDUP_AUTO_THRESHOLD


# ---------------------------------------------------------------------------
# 8. Step name — pins the structured-log / pipeline-composition contract.
# ---------------------------------------------------------------------------


def test_step_name_is_detect_near_duplicate():
    from core_api.pipeline.steps.write.detect_near_duplicate import (
        DetectNearDuplicate,
    )

    assert DetectNearDuplicate().name == "detect_near_duplicate"


# ---------------------------------------------------------------------------
# 9. Pipeline wiring — fast pipeline has the step exactly once, between
#    CheckExactDuplicate and WriteMemoryRow. Strong pipeline does NOT
#    have it (it uses CheckSemanticDuplicate instead).
# ---------------------------------------------------------------------------


def test_fast_pipeline_wires_detect_near_duplicate_exactly_once():
    from core_api.pipeline.compositions.write import build_fast_write_pipeline

    names = [s.name for s in build_fast_write_pipeline()._steps]
    assert names.count("detect_near_duplicate") == 1


def test_fast_pipeline_has_step_after_exact_dedup_and_before_write():
    from core_api.pipeline.compositions.write import build_fast_write_pipeline

    names = [s.name for s in build_fast_write_pipeline()._steps]
    assert "detect_near_duplicate" in names
    assert "check_exact_duplicate" in names
    assert "write_memory_row" in names
    assert names.index("check_exact_duplicate") < names.index("detect_near_duplicate")
    assert names.index("detect_near_duplicate") < names.index("write_memory_row")


def test_strong_pipeline_does_not_include_detect_near_duplicate():
    from core_api.pipeline.compositions.write import build_strong_write_pipeline

    names = [s.name for s in build_strong_write_pipeline()._steps]
    assert "detect_near_duplicate" not in names
    # Strong path keeps the LLM-gated CheckSemanticDuplicate.
    assert "check_semantic_duplicate" in names


def test_fast_pipeline_name_is_write_fast():
    from core_api.pipeline.compositions.write import build_fast_write_pipeline

    p = build_fast_write_pipeline()
    assert p._name == "write_fast"


# ---------------------------------------------------------------------------
# 10. The advisory contract: even a candidate well above AUTO must NOT
#     raise. This is the load-bearing difference vs CheckSemanticDuplicate.
# ---------------------------------------------------------------------------


async def test_high_similarity_candidate_does_not_raise_http_exception():
    from fastapi import HTTPException

    from core_api.pipeline.steps.write.detect_near_duplicate import (
        DetectNearDuplicate,
    )

    ctx = _build_ctx()
    candidate = {
        "id": str(uuid4()),
        "similarity": 0.999,  # would 409 on the strong-side step
        "content": "Near-identical existing memory.",
        "subject_entity_id": None,
    }
    with patch(
        "core_api.pipeline.steps.write.detect_near_duplicate._find_semantic_duplicate",
        new=AsyncMock(return_value=candidate),
    ):
        step = DetectNearDuplicate()
        # The whole point of A21: advisory, never raises. Wrap in a
        # try/except so a stray HTTPException would surface explicitly.
        try:
            result = await step.execute(ctx)
        except HTTPException as exc:  # pragma: no cover — failure shape
            pytest.fail(
                f"DetectNearDuplicate must never raise HTTPException; got {exc.status_code}"
            )
    assert result is None
    # Stash is present so the caller observes the near-dup signal.
    assert ctx.data["memory_fields"]["metadata"]["near_duplicate_of"] == candidate["id"]


# ---------------------------------------------------------------------------
# Rounding contract — similarity stored as 4dp (mirrors the "<float, rounded
# to 4 places>" line in the A21 spec).
# ---------------------------------------------------------------------------


async def test_similarity_rounded_to_four_decimals():
    from core_api.pipeline.steps.write.detect_near_duplicate import (
        DetectNearDuplicate,
    )

    ctx = _build_ctx()
    candidate = {
        "id": str(uuid4()),
        "similarity": 0.973456789,
        "content": "...",
        "subject_entity_id": None,
    }
    with patch(
        "core_api.pipeline.steps.write.detect_near_duplicate._find_semantic_duplicate",
        new=AsyncMock(return_value=candidate),
    ):
        await DetectNearDuplicate().execute(ctx)

    md = ctx.data["memory_fields"]["metadata"]
    # 4dp round — equality, not approx, because the contract is exact.
    assert md["near_duplicate_similarity"] == 0.9735
