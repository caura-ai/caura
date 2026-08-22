"""A junk value in one enrichment field must not discard the whole result.

``EnrichmentResult`` is built from unenforced LLM output and its caller recovers
from a ValidationError by falling through to ``fake_enrich``. For which fields
were already guarded and why these two were not, see ``_coerce_summary`` and
``_coerce_pii_types`` in ``common/enrichment/schema.py``.
"""

from __future__ import annotations

import pytest

from common.enrichment.service import _validate_enrichment

pytestmark = [pytest.mark.unit]


def _raw(**over) -> dict:
    """A well-formed enrichment payload, as the LLM should return it."""
    base = {
        "memory_type": "fact",
        "title": "Anna ships Helios",
        "summary": "Anna shipped the Helios migration.",
        "weight": 0.8,
        "status": "active",
        "tags": ["migration"],
        "contains_pii": False,
        "pii_types": [],
        "business_relevance": "business",
        "retrieval_hint": "helios migration",
    }
    base.update(over)
    return base


# Every shape verified to raise ValidationError before this fix.
_JUNK = [
    pytest.param(5, id="number"),
    pytest.param(None, id="null"),
    pytest.param({"a": 1}, id="object"),
    pytest.param(["x"], id="array"),
]


@pytest.mark.parametrize("junk", _JUNK)
def test_junk_summary_does_not_discard_the_enrichment(junk) -> None:
    """The other nine fields must survive a bad ``summary``."""
    result = _validate_enrichment(_raw(summary=junk), llm_ms=12)

    assert result.summary == "", f"a junk summary must normalise to empty; got {result.summary!r}"
    # The point of the fix: everything else is still the LLM's work, not
    # fake_enrich's.
    assert result.title == "Anna ships Helios"
    assert result.tags == ["migration"]
    assert result.weight == 0.8
    assert result.retrieval_hint == "helios migration"
    assert result.llm_ms == 12


@pytest.mark.parametrize("junk", _JUNK)
def test_junk_title_is_not_persisted_as_its_repr(junk) -> None:
    """A junk ``title`` must normalise to empty, not to ``str(junk)``.

    Unlike ``summary`` / ``pii_types`` above this was never a crash —
    ``_validate_enrichment`` coerced with a bare ``str()``, so ``{'a': 1}``
    reached storage as the string ``"{'a': 1}"`` and ``None`` as ``"None"``.
    Both render in the UI as a title the model never wrote.
    """
    result = _validate_enrichment(_raw(title=junk), llm_ms=12)

    assert result.title == "", (
        f"a junk title must normalise to empty, not to its repr; got {result.title!r}"
    )


@pytest.mark.parametrize(
    "raw_value,expected",
    [
        # A bare string is the LLM answering loosely, not wrongly. Wrapped
        # rather than dropped to preserve audit evidence — the gate itself
        # keys on contains_pii, not on these types.
        ("email", ["email"]),
        ("  ", []),
        ([1, 2], []),
        (["email", None, "phone"], ["email", "phone"]),
        ({"a": 1}, []),
        (None, []),
        (["email"], ["email"]),
    ],
)
def test_pii_types_junk_is_normalised_not_fatal(raw_value, expected) -> None:
    result = _validate_enrichment(_raw(pii_types=raw_value, contains_pii=True), llm_ms=1)

    assert result.pii_types == expected, (
        f"pii_types={raw_value!r} should normalise to {expected!r}; got {result.pii_types!r}"
    )
    # contains_pii is what the gate actually reads, and it is independent of
    # this field — "PII present, type unknown" must still trigger.
    assert result.contains_pii is True
    assert result.title == "Anna ships Helios"


def test_clean_payload_is_untouched() -> None:
    """Control: tolerance must not alter a well-formed enrichment."""
    result = _validate_enrichment(
        _raw(summary="A real summary.", pii_types=["email"], contains_pii=True), llm_ms=7
    )

    assert result.summary == "A real summary."
    assert result.pii_types == ["email"]
    assert result.contains_pii is True
    assert result.title == "Anna ships Helios"
    assert result.tags == ["migration"]

# The non-dict shape guard (CAURA-651) is already covered, with a stronger
# message assertion, by
# tests/test_vertex_response_shape.py::TestEnrichmentValidatorRejectsNonDict.
# Not duplicated here.
#
# Likewise the 80-char ``title`` cap, by
# tests/test_p4_1_llm_fallback.py::TestValidateEnrichment::test_title_truncated_to_80
# — same function via the ``core_api.services.memory_enrichment`` re-export, so
# it already guards the truncation half of the ``title`` coercion above.
