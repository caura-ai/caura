"""L1 relationship-classification tests (A55)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from core_api.services.contradiction import relationship as rel

pytestmark = pytest.mark.unit

SUBJ = "00000000-0000-0000-0000-0000000000aa"


def _mem(**over) -> dict:
    base = {
        "id": "m1",
        "content": "X lives in Haifa",
        "subject_entity_id": None,
        "predicate": None,
        "object_value": None,
    }
    base.update(over)
    return base


# ── deterministic RDF branch (no LLM) ──────────────────────────────────
@pytest.mark.asyncio
async def test_rdf_exact_value_short_circuits_no_llm():
    new = _mem(subject_entity_id=SUBJ, predicate="lives_in", object_value="Haifa")
    cand = _mem(subject_entity_id=SUBJ, predicate="lives_in", object_value="Tel Aviv")
    with patch.object(rel, "_llm_raw", new=AsyncMock()) as llm:
        r, c = await rel.classify_relationship(new, cand)
    assert r == "exact_value"
    assert c == rel._CONF_RDF
    llm.assert_not_called()  # deterministic — LLM never invoked


def test_is_rdf_exact_value_guards():
    # same value -> not exact_value
    assert not rel._is_rdf_exact_value(
        _mem(subject_entity_id=SUBJ, predicate="p", object_value="A"),
        _mem(subject_entity_id=SUBJ, predicate="p", object_value="A"),
    )
    # different subject -> no
    assert not rel._is_rdf_exact_value(
        _mem(subject_entity_id=SUBJ, predicate="p", object_value="A"),
        _mem(subject_entity_id="other", predicate="p", object_value="B"),
    )
    # missing RDF fields -> no
    assert not rel._is_rdf_exact_value(_mem(), _mem())


# ── extract_relationship: model-emitted, each of the 7 ─────────────────
@pytest.mark.parametrize(
    "value",
    [
        "exact_value",
        "negation",
        "entailed",
        "constraint",
        "probabilistic",
        "scope_apparent",
        "refinement",
    ],
)
def test_extract_reads_model_relationship(value):
    assert rel.extract_relationship({"relationship": value}) == (value, "model")


def test_extract_rejects_invalid_relationship_falls_to_derivation():
    # invalid explicit value + a derivable non_conflict_reason
    assert rel.extract_relationship(
        {"relationship": "bogus", "non_conflict_reason": "scope_mismatch"}
    ) == ("scope_apparent", "derived")


# ── extract_relationship: derivation fallback from non_conflict_reason ──
@pytest.mark.parametrize(
    "ncr,expected",
    [
        ("refinement", "refinement"),
        ("scope_mismatch", "scope_apparent"),
        ("list_valued_predicate", "constraint"),
        ("temporal_supersession", "exact_value"),
        ("event_restatement", "entailed"),
        ("conditional_unrealized", "probabilistic"),
    ],
)
def test_extract_derives_from_non_conflict_reason(ncr, expected):
    assert rel.extract_relationship({"non_conflict_reason": ncr}) == (
        expected,
        "derived",
    )


def test_extract_contradicts_true_defaults_exact_value():
    assert rel.extract_relationship(
        {"contradicts": True, "non_conflict_reason": "none"}
    ) == (
        "exact_value",
        "derived",
    )


def test_extract_none_when_nothing_classifiable():
    assert rel.extract_relationship({"contradicts": False}) == (None, "none")
    assert rel.extract_relationship({}) == (None, "none")
    assert rel.extract_relationship("not-a-dict") == (None, "none")


# ── LLM branch: prompt is the EXTENDED copy; model output parsed ───────
@pytest.mark.asyncio
async def test_llm_branch_uses_extended_prompt_and_parses_model():
    new = _mem(content="Alice is inactive")
    cand = _mem(content="Alice is active")
    captured = {}

    async def fake_llm(prompt, tenant_config=None):
        captured["prompt"] = prompt
        return {"contradicts": True, "relationship": "negation"}

    with patch.object(rel, "_llm_raw", new=fake_llm):
        r, c = await rel.classify_relationship(new, cand)

    assert r == "negation"
    assert c == rel._CONF_MODEL
    # The extended instruction is appended to the base prompt (base untouched).
    assert "relationship" in captured["prompt"]
    assert rel.CONTRADICTION_PROMPT.split("{")[0].strip()[:20] in captured["prompt"]
    assert rel._RELATIONSHIP_INSTRUCTION.strip()[:20] in captured["prompt"]


@pytest.mark.asyncio
async def test_llm_branch_derived_confidence_lower():
    new, cand = _mem(content="a"), _mem(content="b")

    async def fake_llm(prompt, tenant_config=None):
        return {"non_conflict_reason": "refinement"}  # no explicit relationship

    with patch.object(rel, "_llm_raw", new=fake_llm):
        r, c = await rel.classify_relationship(new, cand)
    assert r == "refinement"
    assert c == rel._CONF_DERIVED


@pytest.mark.asyncio
async def test_llm_branch_unclassifiable_defaults_exact_value_fallback_conf():
    new, cand = _mem(content="a"), _mem(content="b")

    async def fake_llm(prompt, tenant_config=None):
        return {}  # nothing usable

    with patch.object(rel, "_llm_raw", new=fake_llm):
        r, c = await rel.classify_relationship(new, cand)
    assert r == "exact_value"
    assert c == rel._CONF_FALLBACK
