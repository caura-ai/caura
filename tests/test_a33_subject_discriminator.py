"""A33 ①: re-fold a split-off trailing disambiguator back into its subject.

The extractor inconsistently turns "Acme Corp #0033's …" into a bare subject
"acme corp" + a separate identifier "#0033", collapsing every "<Name> #NNNN"
onto one shared subject entity_id → false same_subject (CAURA-133) + a hub that
dilutes entity_lookup (A30). ``_reattach_subject_discriminators`` undoes that on
the extracted graph, before any resolution. These tests pin the fold rule and
its guards (adjacency-gated; "#"/parenthetical shapes only).
"""

from __future__ import annotations

import pytest

from core_api.services.entity_extraction import (
    ExtractedEntity,
    ExtractedGraph,
    ExtractedRelation,
    Mention,
    _reattach_subject_discriminators,
)

pytestmark = pytest.mark.unit


def _graph(entities, relations=None, mentions=None):
    return ExtractedGraph(
        entities=entities,
        relations=relations or [],
        mentions=mentions or [],
    )


def test_folds_hash_discriminator_into_subject():
    """The dominant A33 case: '#0033' split off 'acme corp' is re-folded."""
    graph = _graph(
        [
            ExtractedEntity(
                canonical_name="acme corp", entity_type="organization", role="subject"
            ),
            ExtractedEntity(
                canonical_name="#0033", entity_type="identifier", role="mentioned"
            ),
        ]
    )
    out = _reattach_subject_discriminators(
        graph, "Acme Corp #0033's employee_count is 13979."
    )
    names = {e.canonical_name for e in out.entities}
    assert names == {"acme corp #0033"}, names
    # the standalone discriminator entity is gone (folded into the subject)
    assert all(e.role == "subject" for e in out.entities)


def test_distinct_siblings_stay_distinct_after_fold():
    """Two siblings that previously both collapsed to 'acme corp' now differ."""
    g28 = _reattach_subject_discriminators(
        _graph(
            [
                ExtractedEntity(
                    canonical_name="acme corp",
                    entity_type="organization",
                    role="subject",
                ),
                ExtractedEntity(
                    canonical_name="#0028", entity_type="identifier", role="mentioned"
                ),
            ]
        ),
        "Acme Corp #0028's employee_count is 48227.",
    )
    g33 = _reattach_subject_discriminators(
        _graph(
            [
                ExtractedEntity(
                    canonical_name="acme corp",
                    entity_type="organization",
                    role="subject",
                ),
                ExtractedEntity(
                    canonical_name="#0033", entity_type="identifier", role="mentioned"
                ),
            ]
        ),
        "Acme Corp #0033's employee_count is 13979.",
    )
    s28 = next(e.canonical_name for e in g28.entities if e.role == "subject")
    s33 = next(e.canonical_name for e in g33.entities if e.role == "subject")
    assert s28 != s33, f"siblings must stay distinct, got {s28!r} and {s33!r}"


def test_fold_remaps_relations_and_mentions():
    """Relation endpoints + mention links that named the bare subject are remapped."""
    graph = _graph(
        [
            ExtractedEntity(
                canonical_name="acme corp", entity_type="organization", role="subject"
            ),
            ExtractedEntity(
                canonical_name="#0033", entity_type="identifier", role="mentioned"
            ),
            ExtractedEntity(
                canonical_name="13979", entity_type="identifier", role="object"
            ),
        ],
        relations=[
            ExtractedRelation(
                from_entity="acme corp",
                relation_type="has_employee_count",
                to_entity="13979",
            )
        ],
        mentions=[
            Mention(
                surface="Acme Corp #0033", cluster_id=0, entity_canonical="acme corp"
            )
        ],
    )
    out = _reattach_subject_discriminators(
        graph, "Acme Corp #0033's employee_count is 13979."
    )
    assert out.relations[0].from_entity == "acme corp #0033"
    assert out.mentions[0].entity_canonical == "acme corp #0033"


def test_folds_parenthetical_qualifier():
    """Generalises beyond '#': a split-off '(delaware)' folds back too."""
    graph = _graph(
        [
            ExtractedEntity(
                canonical_name="acme", entity_type="organization", role="subject"
            ),
            ExtractedEntity(
                canonical_name="(delaware)", entity_type="identifier", role="mentioned"
            ),
        ]
    )
    out = _reattach_subject_discriminators(
        graph, "Acme (Delaware)'s q3_revenue is $10M."
    )
    assert {e.canonical_name for e in out.entities} == {"acme (delaware)"}


def test_no_fold_when_not_adjacent_in_content():
    """The disambiguator only folds if it directly follows the subject in text."""
    graph = _graph(
        [
            ExtractedEntity(
                canonical_name="acme corp", entity_type="organization", role="subject"
            ),
            ExtractedEntity(
                canonical_name="#0033", entity_type="identifier", role="mentioned"
            ),
        ]
    )
    # "#0033" refers to something else here — not adjacent to the subject.
    out = _reattach_subject_discriminators(graph, "Acme Corp depends on widget #0033.")
    names = {e.canonical_name for e in out.entities}
    assert names == {"acme corp", "#0033"}, names


def test_does_not_fold_real_named_identifiers():
    """A real named identifier (not a '#'/paren disambiguator) is never folded,
    even if adjacent — preserves entities like 'pr-2025-a' / 'build-734'."""
    graph = _graph(
        [
            ExtractedEntity(
                canonical_name="acme corp", entity_type="organization", role="subject"
            ),
            ExtractedEntity(
                canonical_name="build-734", entity_type="identifier", role="mentioned"
            ),
        ]
    )
    out = _reattach_subject_discriminators(graph, "Acme Corp build-734 shipped today.")
    names = {e.canonical_name for e in out.entities}
    assert names == {"acme corp", "build-734"}, names


def test_no_subjects_is_noop():
    graph = _graph(
        [
            ExtractedEntity(
                canonical_name="#0033", entity_type="identifier", role="mentioned"
            )
        ]
    )
    out = _reattach_subject_discriminators(graph, "#0033 is here")
    assert {e.canonical_name for e in out.entities} == {"#0033"}
