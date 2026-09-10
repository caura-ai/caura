"""reg-a40 — the deterministic contradiction check could not run where it mattered.

R7 filed this as "Path A semantic defer-and-retry vs base-judge stochasticity":
when entity extraction has not completed, the check falls back to the raw-text
LLM judge and silently misses or false-flags. The mechanism turns out to be
sharper than a race.

Path A's RDF gate needs ``(subject_entity_id, predicate, object_value)``.
``EmitMemoryTriple`` fills those during the write with no LLM call — but it
resolves a SUBJECT from only two sources: a caller-supplied ``entity_links``
row, or an identifier-shaped token. Proper nouns deliberately return ``None``,
deferring to the entity-extraction worker's higher-precision output. So for any
ordinary human- or company-subject fact the row commits with a NULL subject, the
gate fails, and the verdict goes to the stochastic judge.

Extraction then populates the columns moments later and fires Path C — which did
entity-overlap plus another LLM judge and never retried the deterministic check.
The one non-stochastic verdict in the module was structurally unreachable
exactly where it was needed.

The pass is now shared and runs in both places. It is also CHEAPER: a
deterministic verdict short-circuits an LLM judge rather than adding one.
"""

import inspect
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from core_api.services import contradiction_detector as cd

pytestmark = pytest.mark.unit

SUBJECT = "00000000-0000-0000-0000-0000000000aa"


def _memory(**over) -> dict:
    base = {
        "id": str(uuid4()),
        "tenant_id": "t1",
        "fleet_id": "f1",
        "content": "Maya lives in Boston",
        "subject_entity_id": SUBJECT,
        "predicate": "lives_in",
        "object_value": "Boston",
        "deleted_at": None,
        "status": "active",
        "visibility": "scope_team",
        "supersedes_id": None,
        "created_at": "2026-04-29T12:00:00+00:00",
    }
    base.update(over)
    return base


def _candidate(object_value: str, ts: str = "2026-04-29T10:00:00+00:00") -> dict:
    return {
        "id": str(uuid4()),
        "content": f"Maya lives in {object_value}",
        "status": "active",
        "object_value": object_value,
        "created_at": ts,
    }


# ── the premise: why the gate fails at write time ─────────────────────────


@pytest.mark.parametrize(
    "content,expected_subject",
    [
        ("Maya lives in Boston", None),
        ("Acme is headquartered in Berlin", None),
        ("Priya reports to Dana", None),
        ("TOKEN-XYZ has release date 2027", "TOKEN-XYZ"),
    ],
)
def test_write_time_subject_resolution_skips_proper_nouns(content, expected_subject):
    """The root cause, pinned so it cannot change silently.

    Three of these four match a single-value predicate and still resolve NO
    subject, so ``EmitMemoryTriple`` skips and the row commits with a NULL
    ``subject_entity_id``. That is what made the RDF gate unreachable for
    human- and company-subject facts — which is most of them.
    """
    import re

    from core_api.pipeline.steps.write.emit_memory_triple import (
        _PHRASE_TO_PREDICATE,
        _infer_subject_token,
    )

    norm = re.sub(r"\s+", " ", content)
    matches = [(m, p) for pat, p in _PHRASE_TO_PREDICATE for m in pat.finditer(norm)]
    assert matches, "fixture must match a predicate, or it proves nothing"
    head = min(matches, key=lambda mp: mp[0].start())[0]
    assert _infer_subject_token(norm, head) == expected_subject


# ── "could not look" is not "found nothing" ───────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing",
    ["subject_entity_id", "predicate", "object_value"],
)
async def test_pass_reports_it_could_not_run_when_the_triple_is_incomplete(missing):
    """``ran`` exists precisely because this bug hid in the gap between "no
    conflict" and "could not look". Without it, an unpopulated triple and a
    clean bill of health are indistinguishable to every caller."""
    sc = AsyncMock()
    sc.find_rdf_conflicts = AsyncMock(return_value=[])
    mem = _memory(**{missing: None})

    result = await cd._rdf_conflict_pass(
        sc, mem, memory_id=mem["id"], tenant_id="t1", supersedes_id=None
    )

    assert result.ran is False
    assert result.contradictions == []
    sc.find_rdf_conflicts.assert_not_awaited()


@pytest.mark.asyncio
async def test_pass_runs_and_finds_nothing_when_the_triple_is_complete():
    sc = AsyncMock()
    sc.find_rdf_conflicts = AsyncMock(return_value=[])
    mem = _memory()

    result = await cd._rdf_conflict_pass(
        sc, mem, memory_id=mem["id"], tenant_id="t1", supersedes_id=None
    )

    assert result.ran is True
    assert result.contradictions == []
    sc.find_rdf_conflicts.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_non_single_value_predicate_does_not_run_the_pass():
    """``works_on`` can be true of five projects at once — a second value is an
    addition, not a replacement."""
    sc = AsyncMock()
    sc.find_rdf_conflicts = AsyncMock(return_value=[])
    mem = _memory(predicate="works_on")

    result = await cd._rdf_conflict_pass(
        sc, mem, memory_id=mem["id"], tenant_id="t1", supersedes_id=None
    )

    assert result.ran is False
    sc.find_rdf_conflicts.assert_not_awaited()


@pytest.mark.asyncio
async def test_pass_returns_the_conflict_and_the_chain_edge():
    sc = AsyncMock()
    cand = _candidate("Haifa")
    sc.find_rdf_conflicts = AsyncMock(return_value=[cand])
    sc.batch_update_status = AsyncMock(return_value={})
    mem = _memory(object_value="Tel Aviv")

    result = await cd._rdf_conflict_pass(
        sc, mem, memory_id=mem["id"], tenant_id="t1", supersedes_id=None
    )

    assert result.ran is True
    assert len(result.contradictions) == 1
    assert result.contradictions[0].reason == "rdf_conflict"
    # candidate is older, so it is the one retired and the chain points at it
    assert str(result.supersedes_id) == cand["id"]
    assert result.record_pairs and result.record_pairs[0][1] == "rdf"


# ── both triggers reach the same pass ─────────────────────────────────────


def test_write_time_path_delegates_to_the_shared_pass():
    # ``detect_contradictions_async`` is the locking wrapper; ``_detect`` holds
    # the path logic it guards.
    src = inspect.getsource(cd._detect)
    assert "_rdf_conflict_pass(" in src


def test_the_write_time_pass_still_gates_the_semantic_judge():
    """Path A's semantic judge has always been gated on ``if not
    contradictions``. Extraction must not have dropped that, or the RDF path
    would stop saving the LLM call it used to save."""
    src = inspect.getsource(cd._detect)
    assert "if not contradictions:" in src
    assert src.index("_rdf_conflict_pass(") < src.index("if not contradictions:")


def test_post_extraction_path_runs_the_pass_too():
    """The fix. Path C is fired BY entity extraction completing, so it is the
    first moment the triple exists for a proper-noun subject."""
    src = inspect.getsource(cd.detect_contradictions_by_entities_async)
    assert "_rdf_conflict_pass(" in src


def test_the_pass_runs_before_the_entity_overlap_judge():
    """Order is the cost argument. Running the deterministic check first lets a
    hit short-circuit the LLM judge; running it after would spend the call
    anyway and save nothing."""
    src = inspect.getsource(cd.detect_contradictions_by_entities_async)
    assert src.index("_rdf_conflict_pass(") < src.index(
        "find_entity_overlap_candidates"
    )


def test_the_pass_reads_the_refreshed_row_not_the_one_fetched_before_retraction():
    """A4 #13's retraction can clear ``supersedes_id`` and re-fetch. The pass
    must sit after that re-fetch, or it would judge against a stale chain."""
    src = inspect.getsource(cd.detect_contradictions_by_entities_async)
    assert src.index("new_memory = refreshed") < src.index("_rdf_conflict_pass(")


def test_a_deterministic_verdict_short_circuits_the_llm_judge():
    """The no-new-LLM-calls property, asserted on the code rather than assumed:
    Path C returns as soon as the pass produces contradictions, so a
    deterministic verdict REPLACES a judge call instead of preceding one."""
    src = inspect.getsource(cd.detect_contradictions_by_entities_async)
    head = src[: src.index("find_entity_overlap_candidates")]
    assert "if rdf.contradictions:" in head
    assert "return" in head[head.index("if rdf.contradictions:") :]
