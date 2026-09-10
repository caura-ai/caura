"""reg-a35 — a formatting difference was being read as a contradiction.

``memory_find_rdf_conflicts`` selected a conflict with raw string inequality::

    Memory.object_value != object_value

so "7,500 rpm" and "7500 RPM" read as two different values for one
(subject, predicate) pair and the row was flagged as a contradiction it is not.
That is not free: the losing row carries a 0.5 ranking penalty, so a comma
quietly demoted a correct memory.

Both sides are now normalised by the same expression. Shape only — case,
whitespace, thousands separators — and deliberately nothing semantic:
"7500 rpm" and "7500 per minute" still compare as different, because unit
synonymy is open-ended and a wrong equivalence SUPPRESSES a real contradiction,
which is the worse direction to fail in. Same line A65's predicate
canonicaliser draws.
"""

import re

import pytest
from sqlalchemy import literal

from common.models.memory import Memory
from core_storage_api.services.postgres_service import _normalized_object_sql

pytestmark = pytest.mark.unit


def _mirror(v: str) -> str:
    """Python mirror of the SQL expression, for readable pair assertions."""
    return re.sub(r"[\s,]", "", v).lower()


def _sql(v: str) -> str:
    return str(
        _normalized_object_sql(literal(v)).compile(
            compile_kwargs={"literal_binds": True}
        )
    )


# ── what now compares equal ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "a,b",
    [
        ("7,500 rpm", "7500 RPM"),
        ("7500rpm", "7500 rpm"),
        ("Shipped", "shipped"),
        ("BigQuery", "bigquery"),
        ("  in progress ", "In Progress"),
        ("1,000,000", "1000000"),
    ],
)
def test_formatting_differences_no_longer_look_like_conflicts(a, b):
    assert _mirror(a) == _mirror(b)


# ── what deliberately still differs ───────────────────────────────────────


@pytest.mark.parametrize(
    "a,b",
    [
        ("7500 rpm", "7500 per minute"),  # unit synonymy — out of scope on purpose
        ("shipped", "cancelled"),  # a real disagreement
        ("bigquery", "snowflake"),  # the A65 probe case, still a conflict
        ("100", "1000"),  # a comma strip must not equate magnitudes
    ],
)
def test_real_differences_are_still_conflicts(a, b):
    """The dangerous direction. Over-normalising SUPPRESSES a genuine
    contradiction, which is worse than the false positive being fixed — a
    suppressed conflict leaves two contradictory claims both live and current."""
    assert _mirror(a) != _mirror(b)


def test_comma_strip_cannot_merge_different_numbers():
    """``1,000`` and ``1000`` are the same number; ``100`` and ``1000`` are not.
    Stripping separators must not blur that."""
    assert _mirror("1,000") == _mirror("1000")
    assert _mirror("100") != _mirror("1000")


# ── the SQL itself ────────────────────────────────────────────────────────


def test_both_sides_use_the_same_expression():
    """The property that matters. Normalising only the column, or only the
    parameter, would compare a normalised value against a raw one and make the
    mismatch worse rather than better."""
    col = str(
        _normalized_object_sql(Memory.object_value).compile(
            compile_kwargs={"literal_binds": True}
        )
    )
    param = _sql("7,500 RPM")
    for fragment in ("lower(", "regexp_replace("):
        assert fragment in col and fragment in param


def test_the_query_applies_it_to_both_operands():
    import inspect

    from core_storage_api.services.postgres_service import PostgresService

    src = inspect.getsource(PostgresService.memory_find_rdf_conflicts)
    assert src.count("_normalized_object_sql(") == 2
    assert "Memory.object_value != object_value" not in src


def test_object_normalisation_did_not_widen_the_predicate_match():
    """A35 normalises the OBJECT only. It must not blur the predicate as well —
    two attributes that merely format alike are still two attributes.

    Predicate aliasing arrived separately as A36, which replaced the equality
    with a cluster IN. That is a reviewed, enumerated widening; what this test
    still forbids is a normalising expression leaking onto the predicate the way
    it was applied to the object.
    """
    import inspect

    from core_storage_api.services.postgres_service import PostgresService

    src = inspect.getsource(PostgresService.memory_find_rdf_conflicts)
    assert (
        "func.lower(Memory.predicate).in_(sorted(predicate_cluster(predicate)))" in src
    )
    assert "_normalized_object_sql(Memory.predicate)" not in src
