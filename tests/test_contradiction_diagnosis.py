"""L2 diagnosis + combined L1+L2 classifier tests (A55)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from core_api.services.contradiction import diagnosis as dx
from core_api.services.contradiction import relationship as rel

pytestmark = pytest.mark.unit

SUBJ = "00000000-0000-0000-0000-0000000000aa"


def _mem(**over) -> dict:
    base = {
        "id": "m",
        "content": "text",
        "subject_entity_id": None,
        "predicate": None,
        "object_value": None,
    }
    base.update(over)
    return base


# ── extract_diagnosis: model-emitted, each of the 6 ───────────────────
@pytest.mark.parametrize(
    "value",
    [
        "correction",
        "temporal_change",
        "scope_difference",
        "entity_mismatch",
        "write_error",
        "unresolved",
    ],
)
def test_extract_reads_model_diagnosis(value):
    assert dx.extract_diagnosis({"diagnosis": value}, "exact_value") == (value, "model")


# ── extract_diagnosis: derivation from signals + L1 ───────────────────
def test_derive_entity_mismatch_from_same_subject_false():
    assert dx.extract_diagnosis({"same_subject": False}, "negation") == (
        "entity_mismatch",
        "derived",
    )


def test_derive_entity_mismatch_from_same_name_distinct():
    assert dx.extract_diagnosis(
        {"non_conflict_reason": "same_name_distinct_subject"}, "exact_value"
    ) == (
        "entity_mismatch",
        "derived",
    )


def test_derive_temporal_change_from_supersession():
    assert dx.extract_diagnosis(
        {"non_conflict_reason": "temporal_supersession"}, "exact_value"
    ) == (
        "temporal_change",
        "derived",
    )


def test_derive_scope_difference_from_reason_or_relationship():
    assert dx.extract_diagnosis(
        {"non_conflict_reason": "scope_mismatch"}, "exact_value"
    ) == (
        "scope_difference",
        "derived",
    )
    assert dx.extract_diagnosis({}, "scope_apparent") == ("scope_difference", "derived")


def test_derive_correction_from_contradicts_true():
    assert dx.extract_diagnosis({"contradicts": True}, "exact_value") == (
        "correction",
        "derived",
    )
    assert dx.extract_diagnosis({"contradicts": True}, "negation") == (
        "correction",
        "derived",
    )


def test_derive_unresolved_for_both_hold_shapes():
    for r in ("entailed", "constraint", "refinement", "probabilistic"):
        assert dx.extract_diagnosis({}, r) == ("unresolved", "derived")


def test_extract_none_when_unclassifiable():
    assert dx.extract_diagnosis({"contradicts": False}, "exact_value") == (None, "none")
    assert dx.extract_diagnosis("nope", "exact_value") == (None, "none")


# ── combined classify(): RDF deterministic branch (no LLM) ────────────
@pytest.mark.asyncio
async def test_classify_rdf_branch_no_llm():
    new = _mem(subject_entity_id=SUBJ, predicate="lives_in", object_value="Haifa")
    cand = _mem(subject_entity_id=SUBJ, predicate="lives_in", object_value="Tel Aviv")
    with patch.object(dx, "_llm_raw", new=AsyncMock()) as llm:
        res = await dx.classify(new, cand)
    assert res.relationship == "exact_value"
    assert res.relationship_confidence == rel._CONF_RDF
    assert res.diagnosis == "temporal_change"  # RDF default (supersede-preserving)
    assert res.diagnosis_confidence == dx._CONF_RDF_DEFAULT_DIAG
    llm.assert_not_called()


# ── combined classify(): LLM branch, one call yields both ─────────────
@pytest.mark.asyncio
async def test_classify_llm_branch_extracts_both():
    calls = {"n": 0}

    async def fake_llm(prompt, tenant_config=None):
        calls["n"] += 1
        # prompt carries BOTH instructions
        assert "relationship" in prompt and "diagnosis" in prompt
        return {
            "contradicts": True,
            "relationship": "negation",
            "diagnosis": "correction",
        }

    with patch.object(dx, "_llm_raw", new=fake_llm):
        res = await dx.classify(
            _mem(content="Alice inactive"), _mem(content="Alice active")
        )

    assert calls["n"] == 1  # single LLM call for L1 + L2
    assert (
        res.relationship == "negation"
        and res.relationship_confidence == rel._CONF_MODEL
    )
    assert res.diagnosis == "correction" and res.diagnosis_confidence == rel._CONF_MODEL


@pytest.mark.asyncio
async def test_classify_llm_branch_derives_when_fields_absent():
    async def fake_llm(prompt, tenant_config=None):
        return {"non_conflict_reason": "temporal_supersession"}  # no explicit fields

    with patch.object(dx, "_llm_raw", new=fake_llm):
        res = await dx.classify(_mem(content="a"), _mem(content="b"))
    assert res.relationship == "exact_value"  # derived from temporal_supersession
    assert res.diagnosis == "temporal_change"
    assert res.diagnosis_confidence == rel._CONF_DERIVED


@pytest.mark.asyncio
async def test_classify_llm_branch_total_fallback():
    async def fake_llm(prompt, tenant_config=None):
        return {}

    with patch.object(dx, "_llm_raw", new=fake_llm):
        res = await dx.classify(_mem(content="a"), _mem(content="b"))
    assert (
        res.relationship == "exact_value"
        and res.relationship_confidence == rel._CONF_FALLBACK
    )
    assert (
        res.diagnosis == "unresolved" and res.diagnosis_confidence == rel._CONF_FALLBACK
    )
