"""Graph-build fix: literal/attribute values must not become entity nodes (A),
and similarity-merges must not cross distinct identifier suffixes (B).

Regression for the entity_lookup recall collapse at scale: literal/attribute
nodes became high-degree hubs and #NNNN-suffix names merged into contaminated
mega-nodes, exploding entity_lookup's candidate pool.
"""

import pytest

from core_api.services.entity_extraction_worker import (
    _is_valid_entity,
    _same_identifier_signature,
)


@pytest.mark.unit
class TestLiteralAndAttributeRejection:
    """(A) literal values and attribute/field names are not valid entities."""

    @pytest.mark.parametrize(
        "name",
        [
            "2024-03-23",  # ISO date
            "2024-10-16",
            "12/31/2024",  # slashed date
            "95.1%",  # percentage
            "99.0%",
            "14402",  # plain number
            "1935",  # year-as-value
            "$35.4M",  # money
            "$885.3M",
            "sla_uptime",  # snake_case attribute names
            "q3_revenue",
            "founded_year",
            "employee_count",
            "launch_date",
        ],
    )
    def test_literals_and_attributes_rejected(self, name):
        assert not _is_valid_entity(name)

    @pytest.mark.parametrize(
        "name",
        [
            "comet #0002",  # suffixed named entities
            "wayne enterprises #0000",
            "stark industries #0002",
            "mark lin",  # person
            "PR-2025-A",  # legit identifiers (hyphenated, contain letters)
            "build-734",
            "gpt-5.4-nano",
        ],
    )
    def test_real_entities_and_identifiers_pass(self, name):
        assert _is_valid_entity(name)


@pytest.mark.unit
class TestIdentifierSignatureMerge:
    """(B) only merge names that share the same identifier-token signature."""

    @pytest.mark.parametrize(
        "a,b",
        [
            ("comet #0002", "comet #0012"),  # different numeric suffix
            ("wayne enterprises #0000", "wayne enterprises #0017"),
            ("mark lin", "mark lin #0005"),  # bare value vs numbered person
            ("stark industries #0002", "stark industries #0019"),
        ],
    )
    def test_distinct_suffixes_do_not_merge(self, a, b):
        assert not _same_identifier_signature(a, b)

    @pytest.mark.parametrize(
        "a,b",
        [
            ("comet #0002", "comet #0002"),  # identical
            ("openai", "open ai"),  # no digit tokens → allowed to merge (aliasing)
            ("acme corp", "acme corporation"),
        ],
    )
    def test_same_signature_may_merge(self, a, b):
        assert _same_identifier_signature(a, b)
