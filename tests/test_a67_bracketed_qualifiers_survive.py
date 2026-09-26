"""A67 — a bracketed qualifier was dropped from the canonical name, so two
different things with the same base name collapsed into one entity.

``EXTRACTION_PROMPT`` said ``canonical_name: lowercase, no articles`` and
nothing about qualifiers, so "Zylo (Delaware)" and "Zylo (Ohio)" both extracted
as ``zylo``. Once they share an entity, every later statement about either is
attributed to both, and a contradiction between two unrelated organizations
looks like one organization contradicting itself.

Measured against the live Vertex model, same seven sentences through both
prompts, nothing else changed:

    before   "Zylo (Delaware)"  -> zylo            "Zylo (Ohio)"  -> zylo
             "Orbiter (v2)"     -> orbiter859085   "Orbiter (v3)" -> orbiter859085
             -> 2/2 in-scope cases merged
    after    -> zylo859085 (delaware) / (ohio), orbiter859085 (v2) / (v3)
             -> 0/2 merged, and end-to-end 0/3 wrong with 0 fake fallbacks

The NEGATIVE control matters as much as the fix: "Priya at AcmeCorp" and "Priya
at BetaIndustries" must BOTH stay bare ``priya``. The qualifier has to be one
the content brackets — folding in nearby context would split a single person
across every employer she is ever mentioned alongside, which is the opposite
failure and a worse one.

One trap this rule already fell into once, hence
``test_examples_are_complete_entities`` below: the first draft wrote its
examples as ``-> canonical_name: acme (delaware)``, naming a single field. The
model read that as the output shape and stopped emitting ``role``, which is a
required ``str`` on ``ExtractedEntity`` — so ``_parse_graph_lenient`` dropped
EVERY entity, ``_do_extract`` raised on the total loss, and extraction fell
through to the regex heuristic. That heuristic emits bare canonical names, which
is indistinguishable from the very bug under test. Examples in this block must
therefore show a whole entity object.
"""

import json
import re

import pytest

from core_api.services.entity_extraction import EXTRACTION_PROMPT, ExtractedEntity

pytestmark = pytest.mark.unit


def test_prompt_tells_the_model_to_keep_the_qualifier():
    """The bug was the absence of any rule here, so its presence is the fix."""
    assert "KEEP a bracketed qualifier" in EXTRACTION_PROMPT


def test_prompt_explains_the_consequence_of_dropping_it():
    """Keeps the next editor from trimming the paragraph as verbose: removing it
    silently re-merges distinct entities."""
    assert "collapses them into one entity" in EXTRACTION_PROMPT
    assert "attributed to both" in EXTRACTION_PROMPT


def test_prompt_carries_both_sides_of_a_worked_pair():
    """A single example does not show that the qualifier DISCRIMINATES — the
    pair does."""
    assert "acme (delaware)" in EXTRACTION_PROMPT
    assert "acme (ohio)" in EXTRACTION_PROMPT


def test_rule_is_scoped_to_qualifiers_the_content_brackets():
    """Without this bound the rule over-captures: "Priya at AcmeCorp" would
    become a different person from "Priya at BetaIndustries", splitting one
    human across every employer she is mentioned near."""
    assert "Only qualifiers the CONTENT brackets" in EXTRACTION_PROMPT
    assert "Do not invent one" in EXTRACTION_PROMPT
    assert 'still "priya"' in EXTRACTION_PROMPT


def test_examples_are_complete_entities():
    """The regression that cost a full measurement round. Every JSON example in
    the qualifier block must carry all three REQUIRED fields of
    ``ExtractedEntity`` — an example listing only ``canonical_name`` teaches the
    model to omit ``role``, and a payload missing a required field is dropped
    item-by-item until nothing is left and extraction degrades to regex.

    Required fields are read off the model rather than hard-coded, so adding a
    required field to ``ExtractedEntity`` fails here instead of silently
    reintroducing the same collapse.
    """
    block = EXTRACTION_PROMPT[EXTRACTION_PROMPT.index("KEEP a bracketed qualifier") :]
    block = block[: block.index("- relation_type")]

    # The prompt is a .format() template, so its literal braces are doubled.
    examples = re.findall(r"\{\{(.*?)\}\}", block, re.S)
    assert examples, "the qualifier rule lost its worked examples"

    required = {
        name for name, f in ExtractedEntity.model_fields.items() if f.is_required()
    }
    for raw in examples:
        parsed = json.loads("{" + raw + "}")
        missing = required - parsed.keys()
        assert not missing, f"example omits required field(s) {missing}: {parsed}"


def test_examples_use_a_role_the_prompt_offers():
    """An example is only safe to copy if the value it demonstrates is legal."""
    block = EXTRACTION_PROMPT[EXTRACTION_PROMPT.index("KEEP a bracketed qualifier") :]
    block = block[: block.index("- relation_type")]
    for raw in re.findall(r"\{\{(.*?)\}\}", block, re.S):
        assert json.loads("{" + raw + "}")["role"] in {"subject", "object", "mentioned"}
