"""CAURA-720 — the ``weight`` rubric gains its missing 0.0-0.2 band, and the
JSON template stops exemplifying a value the rubric never sanctioned.

**The gap.** ``weight`` is declared ``0.0-1.0`` but the rubric described only
``0.3-1.0``. On the eToro corpus 14.3% of rows landed in the undescribed range
(``0.1`` at 9.3%, ``0.2`` at 4.9%) — the model filling a gap the prompt left,
not violating a stated bound.

**Why the band is documented rather than clamped.** ``0.3`` is not a
prompt-local boundary; it is the system's "low salience" line, and FOUR
consumers read ``weight < 0.3``:

* insights patterns mode      (``weight < 0.3 AND recall_count > 0``)
* insights stale mode
* ``CRYSTALLIZER_STALE_MAX_WEIGHT``  (0.3)
* ``LIFECYCLE_STALE_ARCHIVE_WEIGHT`` (0.3)

All four use strict ``<``. Clamping the floor to 0.3 — the intuitive guard —
would make ``< 0.3`` unsatisfiable for every new memory and silently starve
all four: no error, just empty result sets. So the range is defined instead,
and worded by its CONSEQUENCE (auto-archival) so the model's choice is
informed by what the system does with the number.

**The template value.** ``"weight": 0.0`` sat outside every band the rubric
described, and 32 rows landed on exactly ``0.0`` — below even
``EVOLVE_WEIGHT_FLOOR`` (0.05), so evolve's dampening cannot have produced
them; the model was copying the exemplar. ``0.5`` replaces it as the mid-range
neutral, inside a described band.

Note what is NOT asserted below: that the exemplar equals "the" default
weight. There are two, and they differ — this path falls back to a hardcoded
``0.7`` (``_validate_enrichment``, and ``EnrichmentResult.weight``'s pydantic
default), while ``DEFAULT_MEMORY_WEIGHT`` (0.5) is only reached when no
enrichment value exists at all. Pinning the template to either constant would
create a false coupling: changing ``DEFAULT_MEMORY_WEIGHT`` would fail a test
about the PROMPT.

What this does NOT fix: reproducibility. Two identical writes can still get
different weights — see ``weight_source`` (#1207), which records whether a
value came from the caller, the LLM, or the default precisely because the LLM
case is not repeatable.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _prompt() -> str:
    from common.enrichment._prompts import ENRICHMENT_PROMPT

    return ENRICHMENT_PROMPT


def _template() -> str:
    """The JSON example at the tail of the prompt — what the model
    pattern-matches on, as distinct from the rubric that describes the rules."""
    return _prompt().rsplit("Return ONLY valid JSON", 1)[-1]


# ---------------------------------------------------------------------------
# The band
# ---------------------------------------------------------------------------


def test_rubric_covers_the_bottom_of_the_declared_range():
    """Every part of the declared ``0.0-1.0`` range now has a description."""
    prompt = _prompt()
    assert "0.0-0.2" in prompt, "the 0.0-0.2 band must be described"
    for band in ("0.9-1.0", "0.7-0.8", "0.5-0.6", "0.3-0.4"):
        assert band in prompt, f"pre-existing band {band} disappeared"


def test_band_is_worded_by_consequence_not_by_an_abstract_label():
    """ "Trivial" tells the model nothing about what happens next. The four
    consumers treat sub-0.3 as archivable, so the prompt says so."""
    prompt = _prompt()
    assert "auto-archived" in prompt


def test_the_floor_is_not_clamped_in_validation():
    """The guard this change deliberately did NOT add. A floor of 0.3 would
    make ``weight < 0.3`` unsatisfiable and starve four consumers, so a model
    that returns 0.1 must still land on 0.1."""
    from common.enrichment.service import _validate_enrichment

    result = _validate_enrichment({"memory_type": "fact", "weight": 0.1}, llm_ms=5)
    assert result.weight == pytest.approx(0.1)


@pytest.mark.parametrize("raw,expected", [(0.0, 0.0), (0.2, 0.2), (1.0, 1.0)])
def test_declared_range_round_trips(raw, expected):
    """The whole declared range survives validation unchanged — including the
    endpoints, which the rubric previously left undefined."""
    from common.enrichment.service import _validate_enrichment

    result = _validate_enrichment({"memory_type": "fact", "weight": raw}, llm_ms=5)
    assert result.weight == pytest.approx(expected)


def test_out_of_range_is_still_clamped():
    """Unchanged behaviour: values outside 0.0-1.0 clamp to the bounds."""
    from common.enrichment.service import _validate_enrichment

    assert (
        _validate_enrichment({"memory_type": "fact", "weight": 5.0}, llm_ms=5).weight
        == 1.0
    )
    assert (
        _validate_enrichment({"memory_type": "fact", "weight": -3.0}, llm_ms=5).weight
        == 0.0
    )


def test_non_numeric_weight_still_falls_back():
    """Unchanged behaviour: the defensive coercion for ``"high"`` / ``None``
    that the service has carried since before this change."""
    from common.enrichment.service import _validate_enrichment

    assert (
        _validate_enrichment({"memory_type": "fact", "weight": "high"}, llm_ms=5).weight
        == 0.7
    )
    assert (
        _validate_enrichment({"memory_type": "fact", "weight": None}, llm_ms=5).weight
        == 0.7
    )


# ---------------------------------------------------------------------------
# The template exemplar
# ---------------------------------------------------------------------------


def test_template_exemplar_is_inside_a_described_band():
    template = _template()
    assert '"weight": 0.5' in template
    assert '"weight": 0.0' not in template, (
        "0.0 was outside every described band and was being copied verbatim"
    )


def test_template_exemplar_is_not_pinned_to_either_default_constant():
    """The exemplar must NOT be asserted equal to a default-weight constant.

    There are two, on different paths, and they disagree: this path (the LLM
    answered) falls back to a hardcoded ``0.7`` — in ``_validate_enrichment``
    and again as ``EnrichmentResult.weight``'s pydantic default — while
    ``DEFAULT_MEMORY_WEIGHT`` (0.5) is only reached when no enrichment value
    exists at all.

    An earlier version of this file asserted the template equalled
    ``DEFAULT_MEMORY_WEIGHT``. It passed coincidentally and encoded a false
    claim: changing that constant would have failed a test about the PROMPT and
    pushed someone to edit the prompt to fix it. This test pins the absence of
    that coupling instead.
    """
    from common.enrichment.constants import DEFAULT_MEMORY_WEIGHT
    from common.enrichment.schema import EnrichmentResult
    from common.enrichment.service import _validate_enrichment

    # The two really are different — if a later PR unifies them, this assertion
    # is the one to revisit (and the docstrings that explain the split).
    enrichment_path_fallback = EnrichmentResult().weight
    assert enrichment_path_fallback != DEFAULT_MEMORY_WEIGHT, (
        "the two default weights have converged — update the CAURA-720 "
        "docstrings, which document them as deliberately separate"
    )
    assert _validate_enrichment({"memory_type": "fact"}, llm_ms=1).weight == (
        enrichment_path_fallback
    ), "the validator fallback and the pydantic default must stay in step"


def test_template_keeps_weight_numeric():
    """A quoted placeholder (``"..."``) would model the wrong JSON type. The
    service already carries a coercion for non-numeric strings like ``"high"``,
    which is evidence the model does return them when nudged."""
    import json

    # The object sits on its own line; the tail of the prompt also contains the
    # ``{content}`` format placeholder, so a greedy brace match would overrun.
    line = next(
        (ln.strip() for ln in _template().splitlines() if ln.strip().startswith("{")),
        None,
    )
    assert line, "no JSON object found in the template"
    # The prompt escapes its literal braces for ``str.format``; undo that.
    parsed = json.loads(line.replace("{{", "{").replace("}}", "}"))
    assert isinstance(parsed["weight"], (int, float)), (
        f"template models weight as {type(parsed['weight']).__name__}, not a number"
    )
