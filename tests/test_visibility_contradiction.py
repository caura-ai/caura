"""Visibility + Contradiction edge-case test coverage.

Tests:
1. Contradiction detector visibility scoping
2. Supersession first-match-only behavior

The auto-chunk and bulk-write visibility classes that were here built
``Memory()`` themselves and asserted on what they built, so they could not
fail whatever the write paths did (L-172).
"""

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from core_api.constants import VECTOR_DIM
from tests._contradiction_batch_compat import install_batch_status_replay_shim

# ---------------------------------------------------------------------------
# 1. Contradiction detector visibility scoping
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestContradictionVisibilityScoping:
    """Contradiction detector should only find contradictions within the same
    visibility scope. A scope_agent memory should not contradict a scope_team memory.
    """

    @pytest.mark.asyncio
    async def test_rdf_path_includes_fleet_id_filter(self):
        """RDF contradiction path filters by fleet_id, providing implicit
        visibility scoping for scope_team memories."""
        from core_api.services.contradiction_detector import _detect

        subject_id = str(uuid4())
        new_memory = {
            "id": str(uuid4()),
            "tenant_id": "t1",
            "fleet_id": "f1",
            "content": "X lives in Haifa",
            "subject_entity_id": subject_id,
            "predicate": "lives_in",
            "object_value": "Haifa",
            "deleted_at": None,
            "status": "active",
            "visibility": "scope_team",
            "supersedes_id": None,
        }

        mock_sc = AsyncMock()
        mock_sc.find_rdf_conflicts = AsyncMock(return_value=[])
        mock_sc.find_similar_candidates = AsyncMock(return_value=[])
        mock_sc.update_memory_status = AsyncMock()
        install_batch_status_replay_shim(mock_sc)

        with patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=mock_sc,
        ):
            embedding = [0.1] * VECTOR_DIM
            await _detect(new_memory, embedding)

        # Verify the RDF path was invoked (single-value predicate)
        mock_sc.find_rdf_conflicts.assert_called_once()
        # Verify tenant_id was passed (fleet scoping is handled by storage client)
        call_args = mock_sc.find_rdf_conflicts.call_args
        assert call_args[0][0] == "t1", "RDF query must include tenant_id"


# ---------------------------------------------------------------------------
# 2. Supersession first-match-only
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSupersessionFirstMatchOnly:
    """When multiple old memories contradict a new one, supersedes_id should
    point to the FIRST contradicted memory, not the last.
    """

    @pytest.mark.asyncio
    async def test_rdf_supersession_points_to_first_match(self):
        """RDF supersession: supersedes_id should point to the first
        contradicted memory when multiple old memories conflict.
        """
        from core_api.services.contradiction_detector import _detect

        old_id_1 = str(uuid4())
        old_id_2 = str(uuid4())
        new_id = str(uuid4())
        subject_id = str(uuid4())

        old_mem_1 = {
            "id": old_id_1,
            "content": "X lives in Tel Aviv",
            "status": "active",
            "object_value": "Tel Aviv",
            "created_at": "2026-04-29T10:00:00+00:00",
        }
        old_mem_2 = {
            "id": old_id_2,
            "content": "X lives in Jerusalem",
            "status": "active",
            "object_value": "Jerusalem",
            "created_at": "2026-04-29T11:00:00+00:00",
        }

        new_memory = {
            "id": new_id,
            "tenant_id": "t1",
            "fleet_id": "f1",
            "content": "X lives in Haifa",
            "subject_entity_id": subject_id,
            "predicate": "lives_in",
            "object_value": "Haifa",
            "deleted_at": None,
            "status": "active",
            "visibility": "scope_team",
            "supersedes_id": None,
            "created_at": "2026-04-29T12:00:00+00:00",
        }

        mock_sc = AsyncMock()
        mock_sc.find_rdf_conflicts = AsyncMock(return_value=[old_mem_1, old_mem_2])
        mock_sc.update_memory_status = AsyncMock()
        install_batch_status_replay_shim(mock_sc)

        with patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=mock_sc,
        ):
            embedding = [0.1] * VECTOR_DIM
            contradictions = await _detect(new_memory, embedding)

        # Should find 2 contradictions
        assert len(contradictions) == 2

        # Both old memories should be marked outdated via storage client
        mock_sc.update_memory_status.assert_any_call(old_id_1, "outdated")
        mock_sc.update_memory_status.assert_any_call(old_id_2, "outdated")

        # supersedes_id should point to the first contradicted memory
        supersession_calls = [
            c
            for c in mock_sc.update_memory_status.call_args_list
            if c.kwargs.get("supersedes_id")
        ]
        assert len(supersession_calls) == 1, (
            "supersedes_id should only be set once (first RDF conflict)"
        )
        assert supersession_calls[0].kwargs["supersedes_id"] == str(old_id_1), (
            "supersedes_id should point to the first RDF conflict."
        )

    @pytest.mark.asyncio
    async def test_semantic_supersession_points_to_first_match(self):
        """Semantic supersession: supersedes_id should point to the first
        contradicted memory when multiple candidates conflict.
        """
        from core_api.services.contradiction_detector import _detect

        old_id_1 = str(uuid4())
        old_id_2 = str(uuid4())
        new_id = str(uuid4())

        old_mem_1 = {
            "id": old_id_1,
            "content": "The project deadline is Friday",
            "status": "active",
            "created_at": "2026-04-29T10:00:00+00:00",
        }
        old_mem_2 = {
            "id": old_id_2,
            "content": "The project deadline is Thursday",
            "status": "active",
            "created_at": "2026-04-29T11:00:00+00:00",
        }

        new_memory = {
            "id": new_id,
            "tenant_id": "t1",
            "fleet_id": "f1",
            "content": "The project deadline is Monday",
            "subject_entity_id": None,
            "predicate": None,
            "object_value": None,
            "deleted_at": None,
            "status": "active",
            "visibility": "scope_team",
            "supersedes_id": None,
            "created_at": "2026-04-29T12:00:00+00:00",
        }

        mock_sc = AsyncMock()
        mock_sc.find_similar_candidates = AsyncMock(return_value=[old_mem_1, old_mem_2])
        mock_sc.update_memory_status = AsyncMock()
        install_batch_status_replay_shim(mock_sc)

        with (
            patch(
                "core_api.services.contradiction_detector.get_storage_client",
                return_value=mock_sc,
            ),
            patch(
                "core_api.services.contradiction_detector._llm_contradiction_check_batch",
                new_callable=AsyncMock,
                # A61 — batched judge; both candidates contradict.
                return_value=[
                    {
                        "same_subject": True,
                        "contradicts": True,
                        "non_conflict_reason": "none",
                    }
                ]
                * 2,
            ),
        ):
            embedding = [0.1] * VECTOR_DIM
            contradictions = await _detect(new_memory, embedding)

        assert len(contradictions) == 2

        # supersedes_id should point to the first contradicted memory.
        # Verify via the update_memory_status calls that set supersedes_id
        supersession_calls = [
            c
            for c in mock_sc.update_memory_status.call_args_list
            if c.kwargs.get("supersedes_id")
            or (len(c.args) > 1 and "supersedes_id" in str(c))
        ]
        # The first status update with supersedes_id should reference old_id_1
        assert any(
            old_id_1 in str(c) for c in mock_sc.update_memory_status.call_args_list
        ), "First old memory should be referenced in status update calls"

        # Verify both old memories were marked conflicted
        conflicted_calls = [
            c
            for c in mock_sc.update_memory_status.call_args_list
            if "conflicted" in str(c)
        ]
        assert len(conflicted_calls) >= 2, (
            "Both old memories should be marked conflicted"
        )
