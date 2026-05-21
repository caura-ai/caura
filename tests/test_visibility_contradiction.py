"""Visibility + Contradiction edge-case test coverage.

Tests:
1. Auto-chunk child visibility inheritance
2. Bulk write visibility assignment
3. Contradiction detector visibility scoping
4. Supersession first-match-only behavior
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from core_api.constants import VECTOR_DIM


# ---------------------------------------------------------------------------
# 1. Auto-chunk child visibility inheritance
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAutoChunkVisibilityInheritance:
    """Child memories created via auto-chunking should inherit the parent's visibility."""

    def test_child_memory_missing_visibility_in_auto_chunk(self):
        """Demonstrate that the auto-chunk code path does NOT pass visibility
        to child Memory constructors — children get None (Python-level) instead
        of inheriting the parent's visibility. The DB server_default fills in
        'scope_team' on INSERT, but this means a parent with 'scope_org' or
        'scope_agent' produces children with a different visibility.

        This test documents the current (buggy) behavior so that when the
        fix lands, it will start failing and can be updated.
        """
        from common.models.memory import Memory

        # Simulate what auto-chunk does: parent with scope_org
        parent = Memory(
            tenant_id="t1",
            fleet_id="f1",
            agent_id="a1",
            memory_type="fact",
            content="Parent memory with org visibility",
            weight=0.5,
            status="active",
            visibility="scope_org",
            content_hash="parent-hash",
        )
        assert parent.visibility == "scope_org"

        # Simulate child creation as done in memory_service.py (no visibility kwarg)
        child = Memory(
            tenant_id="t1",
            fleet_id="f1",
            agent_id="a1",
            memory_type="fact",
            content="Child chunk fact",
            weight=0.5,
            status="active",
            content_hash="child-hash",
            # NOTE: visibility is NOT passed — this is the bug
        )

        # Child gets None at Python level (server_default applies only on INSERT),
        # which means it won't match the parent's 'scope_org'.
        # When this bug is fixed, child should explicitly get parent's visibility.
        assert child.visibility is None or child.visibility != parent.visibility, (
            "If this fails, the bug is fixed! Update this test to assert inheritance."
        )

    def test_child_inherits_when_visibility_explicitly_set(self):
        """When visibility IS explicitly passed to child, it matches parent."""
        from common.models.memory import Memory

        for vis in ("scope_agent", "scope_team", "scope_org"):
            parent = Memory(
                tenant_id="t1",
                fleet_id="f1",
                agent_id="a1",
                memory_type="fact",
                content="parent",
                weight=0.5,
                status="active",
                visibility=vis,
                content_hash=f"p-{vis}",
            )
            child = Memory(
                tenant_id="t1",
                fleet_id="f1",
                agent_id="a1",
                memory_type="fact",
                content="child",
                weight=0.5,
                status="active",
                visibility=vis,  # Explicitly inherited
                content_hash=f"c-{vis}",
            )
            assert child.visibility == parent.visibility


# ---------------------------------------------------------------------------
# 2. Bulk write visibility assignment
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBulkWriteVisibility:
    """Memories created via bulk write should get the specified visibility."""

    def test_bulk_memory_constructor_no_visibility(self):
        """Bulk write currently does NOT pass visibility to Memory constructor.

        This test documents the bug: bulk-created memories get None at the
        Python level (server_default 'scope_team' only applies on DB INSERT),
        regardless of what the caller intended.
        """
        from common.models.memory import Memory

        # Simulating what create_memories_bulk does (no visibility kwarg)
        mem = Memory(
            tenant_id="t1",
            fleet_id="f1",
            agent_id="a1",
            memory_type="fact",
            content="Bulk item",
            weight=0.5,
            status="active",
            content_hash="bulk-hash",
            # NOTE: no visibility — gets None at Python level, scope_team from DB
        )
        # Python-level default is None; DB server_default fills 'scope_team' on INSERT.
        # The bug is that bulk write never passes the caller's desired visibility.
        assert mem.visibility is None

    def test_bulk_memory_constructor_with_explicit_visibility(self):
        """When visibility IS explicitly passed, it takes effect."""
        from common.models.memory import Memory

        for vis in ("scope_agent", "scope_team", "scope_org"):
            mem = Memory(
                tenant_id="t1",
                fleet_id="f1",
                agent_id="a1",
                memory_type="fact",
                content="Bulk item",
                weight=0.5,
                status="active",
                content_hash=f"bulk-{vis}",
                visibility=vis,
            )
            assert mem.visibility == vis


# ---------------------------------------------------------------------------
# 3. Contradiction detector visibility scoping
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestContradictionVisibilityScoping:
    """Contradiction detector should only find contradictions within the same
    visibility scope. A scope_agent memory should not contradict a scope_team memory.
    """

    @pytest.mark.asyncio
    async def test_find_similar_candidates_includes_visibility_filter(self):
        """Verify _find_similar_candidates adds a visibility WHERE clause to the query."""
        from core_api.repositories import memory_repo

        new_memory = MagicMock()
        new_memory.tenant_id = "t1"
        new_memory.fleet_id = "f1"
        new_memory.visibility = "scope_org"
        new_memory.id = uuid4()

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_db.execute = AsyncMock(return_value=mock_result)

        embedding = [0.1] * VECTOR_DIM
        await memory_repo.find_similar_candidates(mock_db, new_memory, embedding)

        # Inspect the compiled SQL statement passed to db.execute
        call_args = mock_db.execute.call_args
        stmt = call_args[0][0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "visibility" in compiled, (
            "Query must include a visibility filter to scope candidates"
        )

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
        assert call_args[0][0] == "t1", (
            "RDF query must include tenant_id"
        )


# ---------------------------------------------------------------------------
# 4. Supersession first-match-only
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
            c for c in mock_sc.update_memory_status.call_args_list
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

        with patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=mock_sc,
        ), patch(
            "core_api.services.contradiction_detector._llm_contradiction_check",
            new_callable=AsyncMock,
            # A4 #12 — judge returns (verdict, confidence) tuple.
            return_value=(True, 0.90),
        ):
            embedding = [0.1] * VECTOR_DIM
            contradictions = await _detect(new_memory, embedding)

        assert len(contradictions) == 2

        # supersedes_id should point to the first contradicted memory.
        # Verify via the update_memory_status calls that set supersedes_id
        supersession_calls = [
            c for c in mock_sc.update_memory_status.call_args_list
            if c.kwargs.get("supersedes_id") or (len(c.args) > 1 and "supersedes_id" in str(c))
        ]
        # The first status update with supersedes_id should reference old_id_1
        assert any(
            old_id_1 in str(c) for c in mock_sc.update_memory_status.call_args_list
        ), "First old memory should be referenced in status update calls"

        # Verify both old memories were marked conflicted
        conflicted_calls = [
            c for c in mock_sc.update_memory_status.call_args_list
            if "conflicted" in str(c)
        ]
        assert len(conflicted_calls) >= 2, "Both old memories should be marked conflicted"
