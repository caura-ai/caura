"""A1 #17 — deterministic subject-entity preflight.

A small helper used as a fast gate BEFORE the LLM judge in both
``CheckSemanticDuplicate`` (dedup) and ``detect_contradictions_by_entities_async``
(Path C contradiction). When both sides of a (new memory, candidate)
pair have a non-NULL ``subject_entity_id`` AND those IDs differ, the
pair is definitionally about different real-world subjects — no
judge call needed.

This is also the cheapest path to closing the Path C
first-name-collision follow-up (see ``followup-path-c-judge-first-name-collisions``
memory). The entity extractor produces distinct entity rows for two
memories that happen to share a canonical name like "priya"; A1 #17
catches the (subject_a != subject_b) signal those rows carry and
short-circuits the false-positive judge call.

Conservative: only the symmetric ``both-non-null AND differ`` case
fires. Either side missing → caller falls through to the judge.
"""

from __future__ import annotations


def _subjects_differ_with_certainty(left, right) -> bool:
    """Return True iff both ``left`` and ``right`` are present (non-None)
    AND they refer to different subject entities.

    Accepts ``str`` or ``UUID``; compares by string form so callers
    don't need to normalise. Any None / falsy on either side returns
    False — i.e. "we can't tell deterministically, so fall through".
    """
    if left is None or right is None:
        return False
    return str(left) != str(right)
