"""Enrichment answers with ONE object, not an array of per-item objects.

**The failure.** Memory content that is itself a list — bullets, a JSON array,
JSON Lines, records labelled "Memory 1 / Memory 2" — led gemini-3.1-flash-lite
to classify each item on its own and answer ``[{...}, {...}]``. The providers'
shape guard (CAURA-651) rejects a multi-element array; ``unwrap_singleton_array``
only unwraps a one-element one. The retry re-sends the same prompt at
temperature 0 and gets the same array back, so the memory is stored with the
keyword-heuristic fake enrichment instead. Production lost 465 enrichments
this way between 2026-10-10 and 2026-11-06, up to 41 in a day.

**The gap.** The prompt already has a place for several distinct items —
``atomic_facts`` — but it never said that the content is a single memory, so a
list of records read as a batch to classify.

**Measured** on the production model with the real prompt and 13 synthetic
contents, three runs each. On the old prompt 8 of the 13 answered with an
array. With the one-memory framing all 39 answers were a single object, the
listed items landed in ``atomic_facts``, and the contents that already got an
object kept the same ``atomic_facts`` count.

A model's behaviour cannot be pinned in a unit test, so these pin the two
instructions that produced it.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _prompt() -> str:
    from common.enrichment._prompts import ENRICHMENT_PROMPT

    return ENRICHMENT_PROMPT


def _preamble() -> str:
    """What the model reads before the field list — where it decides what it
    is classifying."""
    return " ".join(_prompt().split('1. "memory_type"', 1)[0].split())


def test_the_content_is_framed_as_one_memory():
    preamble = _preamble()
    assert "is ONE memory" in preamble
    assert "even when it lists several items or records" in preamble
    assert "labels its parts as separate memories" in preamble
    assert "exactly one JSON object" in preamble


def test_the_answer_template_rules_out_an_array():
    # ``Return ONLY valid JSON`` stays the anchor: the CAURA-719 and CAURA-720
    # tests split the prompt on it to find the template.
    tail = _prompt().rsplit("Return ONLY valid JSON", 1)[-1]
    assert tail.startswith(": one object, not an array")
