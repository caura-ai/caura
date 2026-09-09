"""reg-a71 — act on the near-duplicate band instead of only reporting it.

``DetectNearDuplicate`` already finds the nearest stored row on every fast write
(one ANN round-trip, no LLM) and stashes ``near_duplicate_of`` as advice nothing
acts on. So a restated fact accumulates a sibling and BOTH stay live, and recall
sees two rows where one claim exists — the composite-crowding pathology from the
2026-09-08 replay, arriving one duplicate at a time.

With ``dedup.merge_near_duplicates`` on, a hit that is provably the same claim
supersedes its predecessor instead of appending beside it.

The judgement is deterministic and free. The plan this implements describes
asking the enrichment LLM "same claim or complementary?", but enrichment
completes BEFORE the candidate is known (``ParallelEmbedEnrich`` precedes this
step), so that phrasing means a SECOND model call on the latency path fast mode
exists to keep short. ``EmitMemoryTriple`` has already populated
``(subject_entity_id, predicate, object_value)`` with no LLM, and the candidate
arrives carrying the same columns — the triple answers the same question from
data already in hand.
"""

import inspect

import pytest

from core_api.pipeline.steps.write.detect_near_duplicate import _same_claim

pytestmark = pytest.mark.unit


class _New:
    def __init__(self, subject=None, predicate=None, obj=None):
        self.subject_entity_id = subject
        self.predicate = predicate
        self.object_value = obj


def _cand(subject=None, predicate=None, obj=None, status="active"):
    return {
        "subject_entity_id": subject,
        "predicate": predicate,
        "object_value": obj,
        "status": status,
    }


# ── what counts as the same claim ─────────────────────────────────────────


def test_same_subject_and_single_valued_predicate_with_a_new_value_is_a_merge():
    """The case the whole task exists for: a fact whose value moved."""
    assert _same_claim(
        _New("s1", "status", "shipped"), _cand("s1", "status", "in progress")
    )


def test_an_identical_value_is_not_merged():
    """A re-statement adds nothing, and superseding a row with its own content
    is churn. The advisory metadata already records that case."""
    assert not _same_claim(
        _New("s1", "status", "shipped"), _cand("s1", "status", "shipped")
    )


def test_case_and_whitespace_do_not_make_a_new_value():
    assert not _same_claim(
        _New("s1", "status", " Shipped "), _cand("s1", "status", "shipped")
    )


def test_a_different_subject_is_never_the_same_claim():
    """Two subjects can hold the same predicate and value without either
    replacing the other."""
    assert not _same_claim(
        _New("s1", "status", "shipped"), _cand("s2", "status", "blocked")
    )


def test_a_multi_valued_predicate_is_never_merged():
    """The heart of the safety argument. ``works_on`` can be true of five
    projects at once, so a second value ADDS information; only a predicate where
    a subject holds exactly one value makes a new value a replacement."""
    assert not _same_claim(
        _New("s1", "works_on", "project-b"), _cand("s1", "works_on", "project-a")
    )


@pytest.mark.parametrize(
    "new,cand",
    [
        (_New(None, "status", "x"), _cand("s1", "status", "y")),
        (_New("s1", None, "x"), _cand("s1", "status", "y")),
        (_New("s1", "status", "x"), _cand(None, "status", "y")),
        (_New("s1", "status", "x"), _cand("s1", None, "y")),
        (_New("s1", "status", None), _cand("s1", "status", "y")),
        (_New("s1", "status", "x"), _cand("s1", "status", None)),
    ],
)
def test_an_unresolved_triple_proves_nothing(new, cand):
    """Absence must never read as a match — an unresolved triple is the common
    case, and treating it as agreement would merge unrelated rows wholesale."""
    assert not _same_claim(new, cand)


# ── the gate ──────────────────────────────────────────────────────────────


def test_the_flag_defaults_off():
    """It changes what a write PRODUCES, not what it reports: an enabled tenant
    sees its older row move to ``outdated``."""
    from core_api.services.organization_settings import ResolvedConfig

    assert ResolvedConfig(tenant_settings={}).merge_near_duplicates is False


def test_the_flag_is_a_declared_writable_key():
    from core_api.services.organization_settings import _LEAF_TYPES

    assert _LEAF_TYPES["dedup.merge_near_duplicates"] is bool


def test_the_detector_checks_the_flag_before_deciding():
    from core_api.pipeline.steps.write import detect_near_duplicate as d

    src = inspect.getsource(d.DetectNearDuplicate.execute)
    assert 'getattr(tenant_config, "merge_near_duplicates", False)' in src


def test_a_dead_candidate_is_never_superseded():
    """An ``outdated`` or ``conflicted`` row already has an owner; stacking a
    second supersession on one is how a lineage forks."""
    from core_api.pipeline.steps.write import detect_near_duplicate as d

    src = inspect.getsource(d.DetectNearDuplicate.execute)
    assert '("active", "confirmed")' in src
    assert "candidate_not_live" in src


def test_a_declined_merge_says_why():
    """``not_same_claim`` is the common outcome and the one that protects
    information — it needs to be visible, not silent."""
    from core_api.pipeline.steps.write import detect_near_duplicate as d

    src = inspect.getsource(d.DetectNearDuplicate.execute)
    assert "not_same_claim" in src


# ── decide here, act after the write ──────────────────────────────────────


def test_the_detector_only_records_the_intent():
    """It runs BEFORE ``WriteMemoryRow``, so the new row has no id and cannot be
    linked to anything yet."""
    from core_api.pipeline.steps.write import detect_near_duplicate as d

    src = inspect.getsource(d.DetectNearDuplicate.execute)
    assert 'ctx.data["merge_supersedes_id"]' in src
    assert "update_memory_status" not in src, "the detector is writing rows itself"


def test_the_post_write_step_performs_it():
    from core_api.pipeline.steps.write import schedule_background_tasks as sb

    assert callable(sb._merge_near_duplicate)
    assert "_merge_near_duplicate(" in inspect.getsource(
        sb.ScheduleBackgroundTasks.execute
    )


def test_the_link_is_written_before_the_candidate_is_retired():
    """The safety property. Retiring first means a failure between the two
    leaves a row ``outdated`` with nothing standing in its place — the claim
    vanishes from recall. This order leaves both live and linked instead."""
    src = inspect.getsource(
        __import__(
            "core_api.pipeline.steps.write.schedule_background_tasks", fromlist=["x"]
        )._merge_near_duplicate
    )
    link = src.index("supersedes_id=candidate_id")
    retire = src.index('"outdated"')
    assert link < retire


def test_a_merge_failure_never_fails_the_write():
    """The row is already committed and the caller already has its 201."""
    src = inspect.getsource(
        __import__(
            "core_api.pipeline.steps.write.schedule_background_tasks", fromlist=["x"]
        )._merge_near_duplicate
    )
    assert src.count("except Exception:") == 2
    # Statement-level check: the docstring says "Never raises", so a substring
    # search for "raise" matches the promise rather than a violation of it.
    code = [ln.strip() for ln in src.splitlines() if not ln.lstrip().startswith("#")]
    assert not any(ln == "raise" or ln.startswith("raise ") for ln in code)
