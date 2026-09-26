"""A77 — four provenance/containment predicates join ``SINGLE_VALUE_PREDICATES``.

``belongs_to``, ``part_of``, ``created_by`` and ``written_by`` are emitted
constantly by relation extraction (~1,939 rows, 3.0% of all extracted relations
on a 768-tenant / 64,212-relation corpus) and were simply absent from the set.
Membership is not cosmetic — it is the gate on three separate runtime paths, and
each is asserted here through the real consumer rather than against the literal:

1. ``_canonical_predicate`` (A65 write-back). A relation type that does not land
   in the set is returned as ``None`` and the memory row keeps a NULL
   ``predicate``. NULL predicate means the RDF path can never fire for that row,
   whatever else is true of it. This is the step that turns the four into live
   triples at all.
2. ``_detect``'s RDF gate. Only a predicate in the set reaches
   ``find_rdf_conflicts``; everything else falls through to the LLM semantic
   judge. Membership buys a deterministic verdict AND suppresses a model call.
3. ``_same_claim`` (A21 near-duplicate). Membership is what makes a second,
   different object a REPLACEMENT rather than an addition — the strongest
   consequence in the list, because it retires a row.

The negative half matters as much as the positive half: the high-volume
relation types in the same corpus (``uses`` 7,962, ``includes`` 6,113, and the
rest) are genuinely multi-valued, and admitting one would manufacture
contradictions between facts that were never in competition. Those are asserted
absent from all three paths too.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from core_api.constants import SINGLE_VALUE_PREDICATES, VECTOR_DIM
from tests._contradiction_batch_compat import install_batch_status_replay_shim

pytestmark = pytest.mark.unit


# The four A77 adds, with the corpus counts that justified each.
A77_PREDICATES: tuple[str, ...] = (
    "belongs_to",  # 750 relations
    "created_by",  # 685
    "written_by",  # 296
    "part_of",  # 208
)

# The high-volume types measured in the SAME corpus that are genuinely
# multi-valued. Listed with counts so a future widening has to argue with the
# numbers rather than with an unexplained deny-list.
MULTI_VALUE_HIGH_VOLUME: tuple[str, ...] = (
    "uses",  # 7,962
    "includes",  # 6,113
    "offers",  # 1,891
    "has",  # 1,350
    "depends_on",  # 1,001
    "provides",  # 837
    "features",  # 663
    "requires",  # 612
)


# ---------------------------------------------------------------------------
# Membership
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("predicate", A77_PREDICATES)
def test_provenance_predicate_is_single_valued(predicate: str) -> None:
    assert predicate in SINGLE_VALUE_PREDICATES


@pytest.mark.parametrize("predicate", MULTI_VALUE_HIGH_VOLUME)
def test_high_volume_multi_value_predicate_stays_out(predicate: str) -> None:
    """The explicit line A77 draws. A subject uses many libraries and includes
    many features; a second value there is an addition, not a replacement."""
    assert predicate not in SINGLE_VALUE_PREDICATES


def test_inverse_direction_stays_out() -> None:
    """Direction is the test, not the noun. ``created_by`` makes the creator an
    attribute of the subject; ``creates`` points the other way and one creator
    has many creations. Mirrors the ``owned_by`` / ``owns`` split the set
    already maintains."""
    for inverse in ("creates", "writes", "wrote", "authors", "owns", "manages"):
        assert inverse not in SINGLE_VALUE_PREDICATES


# ---------------------------------------------------------------------------
# Consumer 1 — A65 predicate write-back (entity_extraction_worker)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("predicate", A77_PREDICATES)
def test_extracted_relation_now_writes_back_a_predicate(predicate: str) -> None:
    """Before A77 every one of these returned ``None`` and the memory row kept a
    NULL predicate, making the deterministic path unreachable for ~1,939 rows."""
    from core_api.services.entity_extraction_worker import _canonical_predicate

    assert _canonical_predicate(predicate) == predicate


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Created By", "created_by"),
        ("created-by", "created_by"),
        ("  WRITTEN_BY  ", "written_by"),
        ("is_part_of", "part_of"),
        ("belongs to", "belongs_to"),
    ],
)
def test_extractor_surface_forms_normalise_onto_the_new_predicates(
    raw: str, expected: str
) -> None:
    """The extractor answers in free-form prose. Shape normalisation only helps
    if the normalised form is in the set — which is exactly what A77 supplies.
    ``is_part_of`` also exercises the prefix strip."""
    from core_api.services.entity_extraction_worker import _canonical_predicate

    assert _canonical_predicate(raw) == expected


@pytest.mark.parametrize("predicate", MULTI_VALUE_HIGH_VOLUME)
def test_multi_value_relation_still_writes_back_nothing(predicate: str) -> None:
    from core_api.services.entity_extraction_worker import _canonical_predicate

    assert _canonical_predicate(predicate) is None


# ---------------------------------------------------------------------------
# Consumer 2 — the RDF contradiction gate (contradiction_detector)
# ---------------------------------------------------------------------------


def _memory(**kwargs) -> dict:
    return {
        "id": str(kwargs.get("id", uuid4())),
        "tenant_id": "a77-tenant",
        "fleet_id": None,
        "subject_entity_id": str(kwargs["subject_entity_id"]),
        "predicate": kwargs["predicate"],
        "object_value": kwargs["object_value"],
        "content": kwargs.get("content", "A77 provenance memory"),
        "status": "active",
        "deleted_at": None,
        "visibility": "scope_team",
        "supersedes_id": None,
        "created_at": kwargs.get("created_at", "2026-09-15T12:00:00+00:00"),
    }


@pytest.mark.parametrize("predicate", A77_PREDICATES)
async def test_rdf_path_fires_for_provenance_predicate(predicate: str) -> None:
    """The deterministic verdict, and the LLM call it saves. ``find_rdf_conflicts``
    is reached, the older row is retired, and the semantic judge is never
    consulted — Path A's judge is gated on ``if not contradictions``."""
    from core_api.services.contradiction_detector import _detect

    subject_id = uuid4()
    new_mem = _memory(
        subject_entity_id=subject_id, predicate=predicate, object_value="Bob"
    )
    old_mem = _memory(
        subject_entity_id=subject_id,
        predicate=predicate,
        object_value="Alice",
        created_at="2026-09-15T11:00:00+00:00",
    )

    mock_sc = AsyncMock()
    mock_sc.find_rdf_conflicts = AsyncMock(return_value=[old_mem])
    mock_sc.find_similar_candidates = AsyncMock(return_value=[])
    mock_sc.update_memory_status = AsyncMock()
    install_batch_status_replay_shim(mock_sc)

    with patch(
        "core_api.services.contradiction_detector.get_storage_client",
        return_value=mock_sc,
    ):
        contradictions = await _detect(new_mem, [0.1] * VECTOR_DIM)

    mock_sc.find_rdf_conflicts.assert_called_once()
    mock_sc.find_similar_candidates.assert_not_called()
    assert [c.reason for c in contradictions] == ["rdf_conflict"]
    mock_sc.update_memory_status.assert_any_call(old_mem["id"], "outdated")


@pytest.mark.parametrize("predicate", MULTI_VALUE_HIGH_VOLUME)
async def test_rdf_path_still_skipped_for_multi_value_predicate(
    predicate: str,
) -> None:
    from core_api.services.contradiction_detector import _detect

    subject_id = uuid4()
    new_mem = _memory(
        subject_entity_id=subject_id, predicate=predicate, object_value="redis"
    )

    mock_sc = AsyncMock()
    mock_sc.find_rdf_conflicts = AsyncMock(return_value=[])
    mock_sc.find_similar_candidates = AsyncMock(return_value=[])
    mock_sc.update_memory_status = AsyncMock()
    install_batch_status_replay_shim(mock_sc)

    with patch(
        "core_api.services.contradiction_detector.get_storage_client",
        return_value=mock_sc,
    ):
        contradictions = await _detect(new_mem, [0.1] * VECTOR_DIM)

    mock_sc.find_rdf_conflicts.assert_not_called()
    assert contradictions == []


# ---------------------------------------------------------------------------
# Consumer 3 — A21 near-duplicate replacement test (detect_near_duplicate)
# ---------------------------------------------------------------------------


def _claim(subject, predicate: str, object_value: str) -> SimpleNamespace:
    return SimpleNamespace(
        subject_entity_id=subject, predicate=predicate, object_value=object_value
    )


@pytest.mark.parametrize("predicate", A77_PREDICATES)
def test_second_value_reads_as_a_replacement(predicate: str) -> None:
    """The consequence with teeth: a differing object on one of these now
    supersedes the earlier row instead of accumulating beside it."""
    from core_api.pipeline.steps.write.detect_near_duplicate import _same_claim

    subject = str(uuid4())
    candidate = {
        "subject_entity_id": subject,
        "predicate": predicate,
        "object_value": "Alice",
    }
    assert _same_claim(_claim(subject, predicate, "Bob"), candidate) is True
    # Identical object is a re-statement, not a replacement — unchanged by A77.
    assert _same_claim(_claim(subject, predicate, "alice"), candidate) is False


@pytest.mark.parametrize("predicate", MULTI_VALUE_HIGH_VOLUME)
def test_multi_value_predicate_never_reads_as_a_replacement(predicate: str) -> None:
    from core_api.pipeline.steps.write.detect_near_duplicate import _same_claim

    subject = str(uuid4())
    candidate = {
        "subject_entity_id": subject,
        "predicate": predicate,
        "object_value": "postgres",
    }
    assert _same_claim(_claim(subject, predicate, "redis"), candidate) is False


# ---------------------------------------------------------------------------
# A36 interaction — ``part_of`` ends in ``_of`` but is not an inverse
# ---------------------------------------------------------------------------


def test_part_of_joins_no_alias_cluster() -> None:
    """``test_a36_predicate_aliasing`` asserts no ``*_of`` member is clustered,
    because aliasing an inverse would read an org chart as self-contradictory.
    ``part_of`` is not an inverse — "X is part of Y" already makes the parent an
    attribute of X — but it must stay unclustered all the same: nothing in the
    set means the same attribute, and a wrong alias manufactures conflicts."""
    from common.constants import predicate_cluster

    assert predicate_cluster("part_of") == frozenset({"part_of"})
    for predicate in A77_PREDICATES:
        assert predicate_cluster(predicate) == frozenset({predicate})
