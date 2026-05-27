"""Single-value predicate classification for RDF contradiction detection.

Unit tests — no database required.
Validates that:
- SINGLE_VALUE_PREDICATES set is well-formed
- RDF contradiction path only fires for single-value predicates
- Multi-value predicates skip RDF and fall through to semantic check
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from core_api.constants import (
    SINGLE_VALUE_PREDICATES,
    VECTOR_DIM,
)
from tests._contradiction_batch_compat import install_batch_status_replay_shim


# ---------------------------------------------------------------------------
# Constants sanity
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSingleValuePredicatesConstants:
    def test_set_is_nonempty(self):
        assert len(SINGLE_VALUE_PREDICATES) > 0

    def test_set_is_frozenset(self):
        """Must be immutable."""
        assert isinstance(SINGLE_VALUE_PREDICATES, frozenset)

    def test_all_lowercase(self):
        """All predicates should be lowercase for case-insensitive matching."""
        for p in SINGLE_VALUE_PREDICATES:
            assert p == p.lower(), f"Predicate '{p}' is not lowercase"

    def test_no_empty_strings(self):
        for p in SINGLE_VALUE_PREDICATES:
            assert len(p.strip()) > 0, "Empty predicate found"

    def test_no_whitespace(self):
        for p in SINGLE_VALUE_PREDICATES:
            assert " " not in p, f"Predicate '{p}' contains whitespace"

    def test_known_single_value_predicates_present(self):
        """Core single-value predicates must be in the set."""
        expected = {
            "lives_in",
            "located_in",
            "reports_to",
            "owned_by",
            "scored",
            "rated",
            "priced_at",
            "status",
            "version",
            "scheduled_for",
            "assigned_to",
            "employed_by",
        }
        for p in expected:
            assert p in SINGLE_VALUE_PREDICATES, (
                f"Expected '{p}' in SINGLE_VALUE_PREDICATES"
            )

    def test_known_multi_value_predicates_absent(self):
        """Core multi-value predicates must NOT be in the set."""
        multi_value = {
            "works_on",
            "uses",
            "created_by",
            "authored_by",
            "manages",
            "depends_on",
            "contains",
            "belongs_to",
            "mentions",
            "leads",
            "owns",
            "founded_by",
            "affiliated_with",
            "approved_by",
            "reviewed_by",
            "responsible_for",
        }
        for p in multi_value:
            assert p not in SINGLE_VALUE_PREDICATES, (
                f"Multi-value predicate '{p}' should not be in SINGLE_VALUE_PREDICATES"
            )

    def test_minimum_coverage(self):
        """Should have broad coverage — at least 150 predicates."""
        assert len(SINGLE_VALUE_PREDICATES) >= 150

    def test_has_prefix_variants_present(self):
        """LLMs often produce has_X — ensure common variants are covered."""
        has_variants = [
            "has_status",
            "has_score",
            "has_rating",
            "has_price",
            "has_role",
            "has_version",
            "has_email",
            "has_phone",
            "has_budget",
            "has_deadline",
            "has_priority",
        ]
        for p in has_variants:
            assert p in SINGLE_VALUE_PREDICATES, f"Missing has_ variant: '{p}'"

    def test_bare_noun_variants_present(self):
        """LLMs may produce bare nouns instead of verb forms."""
        bare_nouns = [
            "score",
            "price",
            "cost",
            "rating",
            "rank",
            "budget",
            "salary",
            "version",
            "deadline",
            "priority",
            "severity",
            "email",
            "phone",
            "location",
            "balance",
            "capacity",
        ]
        for p in bare_nouns:
            assert p in SINGLE_VALUE_PREDICATES, f"Missing bare noun: '{p}'"

    def test_current_prefix_variants_present(self):
        """current_X variants for state predicates."""
        current_variants = [
            "current_status",
            "current_version",
            "current_location",
            "current_role",
            "current_price",
            "current_phase",
            "current_state",
        ]
        for p in current_variants:
            assert p in SINGLE_VALUE_PREDICATES, f"Missing current_ variant: '{p}'"


# ---------------------------------------------------------------------------
# RDF path gating — unit tests with mocked Memory objects
# ---------------------------------------------------------------------------


def _make_memory(**kwargs):
    """Create a dict memory with given attributes."""
    return {
        "id": str(kwargs.get("id", uuid4())),
        "tenant_id": kwargs.get("tenant_id", "test-tenant"),
        "fleet_id": kwargs.get("fleet_id", None),
        "subject_entity_id": str(kwargs["subject_entity_id"]) if kwargs.get("subject_entity_id") else None,
        "predicate": kwargs.get("predicate", "lives_in"),
        "object_value": kwargs.get("object_value", "Tel Aviv"),
        "content": kwargs.get("content", "Test memory content for contradiction"),
        "status": kwargs.get("status", "active"),
        "deleted_at": None,
        "visibility": kwargs.get("visibility", "scope_team"),
        "supersedes_id": None,
        "created_at": kwargs.get("created_at", "2026-04-29T12:00:00+00:00"),
    }


@pytest.mark.unit
class TestRdfPathGating:
    """Verify that _detect only runs RDF path for single-value predicates."""

    async def test_single_value_predicate_triggers_rdf(self):
        """lives_in is single-value — RDF path should execute and find conflict."""
        from core_api.services.contradiction_detector import _detect

        subject_id = uuid4()
        new_mem = _make_memory(
            predicate="lives_in",
            object_value="Haifa",
            subject_entity_id=subject_id,
        )
        old_mem = _make_memory(
            predicate="lives_in",
            object_value="Tel Aviv",
            subject_entity_id=subject_id,
            created_at="2026-04-29T11:00:00+00:00",
        )

        mock_sc = AsyncMock()
        mock_sc.find_rdf_conflicts = AsyncMock(return_value=[old_mem])
        mock_sc.update_memory_status = AsyncMock()
        install_batch_status_replay_shim(mock_sc)

        with patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=mock_sc,
        ):
            contradictions = await _detect(new_mem, [0.1] * VECTOR_DIM)

        # RDF found conflict, semantic skipped
        mock_sc.find_rdf_conflicts.assert_called_once()
        mock_sc.find_similar_candidates.assert_not_called()
        assert len(contradictions) == 1
        assert contradictions[0].reason == "rdf_conflict"
        mock_sc.update_memory_status.assert_any_call(old_mem["id"], "outdated")

    async def test_multi_value_predicate_skips_rdf(self):
        """works_on is multi-value — RDF path should NOT execute."""
        from core_api.services.contradiction_detector import _detect

        subject_id = uuid4()
        new_mem = _make_memory(
            predicate="works_on",
            object_value="Project B",
            subject_entity_id=subject_id,
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

        # RDF should NOT run for multi-value predicates; only semantic path
        mock_sc.find_rdf_conflicts.assert_not_called()
        assert len(contradictions) == 0

    async def test_case_insensitive_predicate_match(self):
        """Predicate matching should be case-insensitive."""
        from core_api.services.contradiction_detector import _detect

        subject_id = uuid4()
        new_mem = _make_memory(
            predicate="Lives_In",  # mixed case
            object_value="Haifa",
            subject_entity_id=subject_id,
        )
        old_mem = _make_memory(
            predicate="Lives_In",
            object_value="Tel Aviv",
            subject_entity_id=subject_id,
            created_at="2026-04-29T11:00:00+00:00",
        )

        mock_sc = AsyncMock()
        mock_sc.find_rdf_conflicts = AsyncMock(return_value=[old_mem])
        mock_sc.update_memory_status = AsyncMock()
        install_batch_status_replay_shim(mock_sc)

        with patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=mock_sc,
        ):
            contradictions = await _detect(new_mem, [0.1] * VECTOR_DIM)

        mock_sc.find_rdf_conflicts.assert_called_once()
        mock_sc.find_similar_candidates.assert_not_called()
        assert len(contradictions) == 1
        assert contradictions[0].reason == "rdf_conflict"

    async def test_no_rdf_triple_skips_rdf_path(self):
        """Memory without subject/predicate/object skips RDF entirely."""
        from core_api.services.contradiction_detector import _detect

        new_mem = _make_memory(
            subject_entity_id=None,
            predicate=None,
            object_value=None,
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

        # Only semantic path runs (no RDF triple)
        mock_sc.find_rdf_conflicts.assert_not_called()
        assert len(contradictions) == 0

    async def test_semantic_path_skipped_when_rdf_finds_contradiction(self):
        """Semantic path should NOT run when RDF already found contradictions."""
        from core_api.services.contradiction_detector import _detect

        subject_id = uuid4()
        new_mem = _make_memory(
            predicate="scored",
            object_value="8.5",
            subject_entity_id=subject_id,
        )
        old_mem = _make_memory(
            predicate="scored",
            object_value="6.2",
            subject_entity_id=subject_id,
            created_at="2026-04-29T11:00:00+00:00",
        )

        mock_sc = AsyncMock()
        mock_sc.find_rdf_conflicts = AsyncMock(return_value=[old_mem])
        mock_sc.update_memory_status = AsyncMock()
        install_batch_status_replay_shim(mock_sc)

        with patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=mock_sc,
        ):
            contradictions = await _detect(new_mem, [0.1] * VECTOR_DIM)

        # Only RDF query should have run — semantic skipped
        mock_sc.find_rdf_conflicts.assert_called_once()
        mock_sc.find_similar_candidates.assert_not_called()
        assert len(contradictions) == 1
        assert contradictions[0].reason == "rdf_conflict"


# ---------------------------------------------------------------------------
# Predicate classification coverage
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPredicateClassification:
    """Verify specific predicates are correctly classified."""

    @pytest.mark.parametrize(
        "predicate",
        [
            # Original core set
            "lives_in",
            "located_in",
            "headquartered_in",
            "based_in",
            "reports_to",
            "led_by",
            "owned_by",
            "managed_by",
            "assigned_to",
            "scored",
            "rated",
            "priced_at",
            "market_cap",
            "potential_score",
            "version",
            "latest_version",
            "replaced_by",
            "replaces",
            "status",
            "phase",
            "state",
            # has_X variants
            "has_score",
            "has_status",
            "has_price",
            "has_role",
            # Bare nouns
            "score",
            "price",
            "rank",
            "budget",
            "priority",
            "severity",
            "balance",
            # current_X variants
            "current_status",
            "current_version",
            "current_location",
            # Domain-specific
            "hostname",
            "environment",
            "tier",
            "subscription",
            "sentiment_score",
            "trading_volume",
            "burn_rate",
            "role",
            "title",
            "scheduled_for",
            "due_by",
            "deadline",
            "eta",
            "configured_as",
            "set_to",
            "backed_by",
            "powered_by",
            "email",
            "phone",
            "employed_by",
            "married_to",
        ],
    )
    def test_single_value(self, predicate):
        assert predicate in SINGLE_VALUE_PREDICATES

    @pytest.mark.parametrize(
        "predicate",
        [
            "works_on",
            "uses",
            "created_by",
            "authored_by",
            "manages",
            "depends_on",
            "belongs_to",
            "part_of",
            "contains",
            "mentions",
            "related_to",
            "leads",
            "owns",
            "founded_by",
            "affiliated_with",
            "approved_by",
            "reviewed_by",
            "responsible_for",
            "plan",
            "blocker",
            "blocked_by",
            "url",
            "endpoint",
            "port",
            "ip_address",
            "domain",
        ],
    )
    def test_multi_value(self, predicate):
        assert predicate not in SINGLE_VALUE_PREDICATES
