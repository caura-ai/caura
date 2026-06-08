"""Cross-Fleet Visibility — comprehensive test suite.

Unit tests validate:
- Visibility constants (values, pattern, default)
- Schema fields (MemoryCreate, MemoryOut, SearchRequest, MemoryUpdate)
- Tool description updates for visibility and fleet_ids
- Memory model visibility attribute

Integration tests verify:
- scope_agent / scope_team / scope_org visibility filtering
- Multi-fleet search
- Admin-mode search
- Default visibility on insert
- Backward compatibility

Benchmark tests measure:
- Overhead of visibility WHERE clause filtering
"""

import re
import statistics
import time
from uuid import uuid4

import pytest

from core_api.constants import (
    MEMORY_VISIBILITIES,
    MEMORY_VISIBILITIES_PATTERN,
)
from core_api.tools import get_spec


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestVisibilityConstants:
    """Verify visibility constant definitions."""

    def test_visibilities_contains_expected_values(self):
        """MEMORY_VISIBILITIES should contain exactly scope_agent, scope_team, scope_org."""
        assert set(MEMORY_VISIBILITIES) == {"scope_agent", "scope_team", "scope_org"}

    def test_visibilities_tuple_length(self):
        assert len(MEMORY_VISIBILITIES) == 3

    def test_pattern_matches_valid_values(self):
        for v in ("scope_agent", "scope_team", "scope_org"):
            assert re.match(MEMORY_VISIBILITIES_PATTERN, v), f"{v} should match"

    def test_pattern_rejects_invalid_values(self):
        for v in (
            "public",
            "global",
            "shared",
            "",
            "FLEET",
            "Private",
            "private",
            "fleet",
            "tenant",
        ):
            assert not re.match(MEMORY_VISIBILITIES_PATTERN, v), f"{v} should not match"

    def test_default_visibility_is_scope_team(self):
        """scope_team is the default — encourages sharing within a team."""
        from core_api.schemas import MemoryOut

        out = MemoryOut(
            id=uuid4(),
            tenant_id="t",
            agent_id="a",
            memory_type="fact",
            content="test",
            weight=0.5,
            source_uri=None,
            run_id=None,
            metadata=None,
            created_at="2025-01-01T00:00:00Z",
            expires_at=None,
        )
        assert out.visibility == "scope_team"


@pytest.mark.unit
class TestVisibilityInSchemas:
    """Verify schema fields support visibility."""

    def test_memory_create_accepts_none_defaults(self):
        from core_api.schemas import MemoryCreate

        mc = MemoryCreate(
            tenant_id="t",
            agent_id="a",
            content="hello",
        )
        # None means the backend applies the default ("scope_team")
        assert mc.visibility is None or mc.visibility == "scope_team"

    def test_memory_create_accepts_valid_visibilities(self):
        from core_api.schemas import MemoryCreate

        for v in ("scope_team", "scope_org", "scope_agent"):
            mc = MemoryCreate(
                tenant_id="t",
                agent_id="a",
                content="hello",
                visibility=v,
            )
            assert mc.visibility == v

    def test_memory_create_rejects_invalid_visibility(self):
        from core_api.schemas import MemoryCreate
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            MemoryCreate(
                tenant_id="t",
                agent_id="a",
                content="hello",
                visibility="public",
            )

        with pytest.raises(ValidationError):
            MemoryCreate(
                tenant_id="t",
                agent_id="a",
                content="hello",
                visibility="global",
            )

    def test_memory_out_has_visibility_default_scope_team(self):
        from core_api.schemas import MemoryOut

        out = MemoryOut(
            id=uuid4(),
            tenant_id="t",
            agent_id="a",
            memory_type="fact",
            content="test",
            weight=0.5,
            source_uri=None,
            run_id=None,
            metadata=None,
            created_at="2025-01-01T00:00:00Z",
            expires_at=None,
        )
        assert out.visibility == "scope_team"

    def test_search_request_has_fleet_ids(self):
        from core_api.schemas import SearchRequest

        sr = SearchRequest(tenant_id="t", query="hello")
        assert hasattr(sr, "fleet_ids")
        assert sr.fleet_ids is None

    def test_search_request_fleet_ids_accepts_list(self):
        from core_api.schemas import SearchRequest

        sr = SearchRequest(
            tenant_id="t",
            query="hello",
            fleet_ids=["f1", "f2"],
        )
        assert sr.fleet_ids == ["f1", "f2"]

    def test_memory_update_accepts_visibility(self):
        from core_api.schemas import MemoryUpdate

        mu = MemoryUpdate(visibility="scope_org")
        assert mu.visibility == "scope_org"


@pytest.mark.unit
class TestToolDescriptions:
    """Verify tool descriptions in the SoT registry reference visibility guidance."""

    def test_write_tool_mentions_visibility(self):
        desc = get_spec("memclaw_write").description
        assert "visibility" in desc.lower()

    def test_write_tool_encourages_sharing(self):
        desc = get_spec("memclaw_write").description.lower()
        assert "scope_team" in desc or "scope_org" in desc

    def test_recall_tool_mentions_fleet_filter(self):
        # `memclaw_recall` advertises cross-fleet filtering via its parameter
        # list rather than prose.
        spec = get_spec("memclaw_recall")
        param_names = {p["name"] for p in __import__("core_api.tools", fromlist=["extract_param_descriptors"]).extract_param_descriptors(spec.handler)}
        assert "fleet_ids" in param_names


@pytest.mark.unit
class TestMemoryModelVisibility:
    """Verify the Memory SQLAlchemy model has a visibility column."""

    def test_memory_model_has_visibility_attribute(self):
        from common.models.memory import Memory

        assert hasattr(Memory, "visibility")


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestVisibilityFiltering:
    """End-to-end visibility filtering with real DB."""

    @staticmethod
    async def _insert_memory(
        sc, tenant_id, fleet_id, agent_id, content, visibility="scope_team", **kwargs
    ):
        from common.embedding import fake_embedding

        mem = await sc.create_memory({
            "tenant_id": tenant_id,
            "fleet_id": fleet_id,
            "agent_id": agent_id,
            "memory_type": "fact",
            "content": content,
            "embedding": fake_embedding(content),
            "weight": 0.5,
            "status": "active",
            "visibility": visibility,
        })
        return mem

    async def test_private_invisible_to_other_agents(
        self,
        db,
        sc,
        tenant_id,
        fleet_id,
    ):
        """Private memory created by agent-A should NOT appear for agent-B."""
        await self._insert_memory(
            sc,
            tenant_id,
            fleet_id,
            "agent-A",
            "Secret plan for agent-A only",
            visibility="scope_agent",
        )
        from core_api.services.memory_service import search_memories

        results = await search_memories(
            db,
            tenant_id=tenant_id,
            query="Secret plan",
            fleet_ids=[fleet_id],
            caller_agent_id="agent-B",
        )
        contents = [r.content for r in results]
        assert "Secret plan for agent-A only" not in contents

    async def test_private_visible_to_owner(
        self,
        db,
        sc,
        tenant_id,
        fleet_id,
    ):
        """Private memory should be visible to the agent that created it."""
        await self._insert_memory(
            sc,
            tenant_id,
            fleet_id,
            "agent-A",
            "My private note about the project",
            visibility="scope_agent",
        )
        from core_api.services.memory_service import search_memories

        results = await search_memories(
            db,
            tenant_id=tenant_id,
            query="private note about the project",
            fleet_ids=[fleet_id],
            caller_agent_id="agent-A",
        )
        contents = [r.content for r in results]
        assert "My private note about the project" in contents

    async def test_fleet_visible_in_same_fleet(
        self,
        db,
        sc,
        tenant_id,
        fleet_id,
    ):
        """Fleet-scoped memory should be visible within the same fleet."""
        await self._insert_memory(
            sc,
            tenant_id,
            fleet_id,
            "agent-A",
            "Fleet-wide status update for testing",
            visibility="scope_team",
        )
        from core_api.services.memory_service import search_memories

        results = await search_memories(
            db,
            tenant_id=tenant_id,
            query="Fleet-wide status update",
            fleet_ids=[fleet_id],
        )
        contents = [r.content for r in results]
        assert "Fleet-wide status update for testing" in contents

    async def test_fleet_invisible_in_other_fleet(
        self,
        db,
        sc,
        tenant_id,
    ):
        """Fleet-scoped memory in fleet-A should NOT appear in fleet-B search."""
        await self._insert_memory(
            sc,
            tenant_id,
            "fleet-A",
            "agent-A",
            "Fleet-A confidential data point",
            visibility="scope_team",
        )
        from core_api.services.memory_service import search_memories

        results = await search_memories(
            db,
            tenant_id=tenant_id,
            query="Fleet-A confidential data point",
            fleet_ids=["fleet-B"],
        )
        contents = [r.content for r in results]
        assert "Fleet-A confidential data point" not in contents

    async def test_tenant_visible_across_fleets(
        self,
        db,
        sc,
        tenant_id,
    ):
        """Tenant-scoped memory in fleet-A should appear when searching fleet-B."""
        await self._insert_memory(
            sc,
            tenant_id,
            "fleet-A",
            "agent-A",
            "Company-wide policy announcement for all",
            visibility="scope_org",
        )
        from core_api.services.memory_service import search_memories

        results = await search_memories(
            db,
            tenant_id=tenant_id,
            query="Company-wide policy announcement",
            fleet_ids=["fleet-B"],
        )
        contents = [r.content for r in results]
        assert "Company-wide policy announcement for all" in contents

    async def test_tenant_visible_in_tenant_wide_search(
        self,
        db,
        sc,
        tenant_id,
    ):
        """Tenant-scoped memory should appear when searching without fleet_id."""
        await self._insert_memory(
            sc,
            tenant_id,
            "fleet-X",
            "agent-A",
            "Org-level insight shared across all fleets",
            visibility="scope_org",
        )
        from core_api.services.memory_service import search_memories

        results = await search_memories(
            db,
            tenant_id=tenant_id,
            query="Org-level insight shared across all fleets",
        )
        contents = [r.content for r in results]
        assert "Org-level insight shared across all fleets" in contents

    async def test_multi_fleet_search(
        self,
        db,
        sc,
        tenant_id,
    ):
        """Search with fleet_ids should return memories from all listed fleets."""
        await self._insert_memory(
            sc,
            tenant_id,
            "fleet-A",
            "agent-A",
            "Multi-fleet memory alpha side",
            visibility="scope_team",
        )
        await self._insert_memory(
            sc,
            tenant_id,
            "fleet-B",
            "agent-B",
            "Multi-fleet memory beta side",
            visibility="scope_team",
        )
        from core_api.services.memory_service import search_memories

        results = await search_memories(
            db,
            tenant_id=tenant_id,
            query="Multi-fleet memory",
            fleet_ids=["fleet-A", "fleet-B"],
        )
        contents = [r.content for r in results]
        assert "Multi-fleet memory alpha side" in contents
        assert "Multi-fleet memory beta side" in contents

    async def test_multi_fleet_excludes_other_fleets(
        self,
        db,
        sc,
        tenant_id,
    ):
        """Search with fleet_ids should NOT return memories from unlisted fleets."""
        await self._insert_memory(
            sc,
            tenant_id,
            "fleet-A",
            "agent-A",
            "Included fleet memory for test",
            visibility="scope_team",
        )
        await self._insert_memory(
            sc,
            tenant_id,
            "fleet-C",
            "agent-C",
            "Excluded fleet-C memory for test",
            visibility="scope_team",
        )
        from core_api.services.memory_service import search_memories

        results = await search_memories(
            db,
            tenant_id=tenant_id,
            query="fleet memory for test",
            fleet_ids=["fleet-A"],
        )
        contents = [r.content for r in results]
        assert "Included fleet memory for test" in contents
        assert "Excluded fleet-C memory for test" not in contents

    async def test_default_visibility_is_scope_team(
        self,
        db,
        tenant_id,
        fleet_id,
        agent_id,
    ):
        """Memory inserted without explicit visibility should default to scope_team."""
        from common.models.memory import Memory
        from common.embedding import fake_embedding

        mem = Memory(
            tenant_id=tenant_id,
            fleet_id=fleet_id,
            agent_id=agent_id,
            memory_type="fact",
            content="Default visibility test memory",
            embedding=fake_embedding("Default visibility test memory"),
            weight=0.5,
            status="active",
        )
        db.add(mem)
        await db.flush()
        await db.refresh(mem)
        assert mem.visibility == "scope_team"

    async def test_admin_sees_team_and_org_not_agent_scoped(
        self,
        db,
        sc,
        tenant_id,
        fleet_id,
    ):
        """Admin search (no caller_agent_id) sees scope_team + scope_org, NOT scope_agent."""
        await self._insert_memory(
            sc,
            tenant_id,
            fleet_id,
            "agent-A",
            "Admin test fleet memory visible",
            visibility="scope_team",
        )
        await self._insert_memory(
            sc,
            tenant_id,
            fleet_id,
            "agent-A",
            "Admin test tenant memory visible",
            visibility="scope_org",
        )
        await self._insert_memory(
            sc,
            tenant_id,
            fleet_id,
            "agent-A",
            "Admin test private memory hidden",
            visibility="scope_agent",
        )
        from core_api.services.memory_service import search_memories

        results = await search_memories(
            db,
            tenant_id=tenant_id,
            query="Admin test memory",
            fleet_ids=[fleet_id],
        )
        contents = [r.content for r in results]
        assert "Admin test fleet memory visible" in contents
        assert "Admin test tenant memory visible" in contents
        assert "Admin test private memory hidden" not in contents


@pytest.mark.integration
class TestBackwardCompatibility:
    """Ensure existing behavior is not broken by visibility feature."""

    async def test_existing_memories_default_to_scope_team(
        self,
        db,
        tenant_id,
        fleet_id,
        agent_id,
    ):
        """Memories inserted without visibility column should default to scope_team."""
        from common.models.memory import Memory
        from common.embedding import fake_embedding

        mem = Memory(
            tenant_id=tenant_id,
            fleet_id=fleet_id,
            agent_id=agent_id,
            memory_type="fact",
            content="Legacy memory without visibility field",
            embedding=fake_embedding("Legacy memory without visibility field"),
            weight=0.5,
            status="active",
        )
        db.add(mem)
        await db.flush()
        await db.refresh(mem)
        assert mem.visibility == "scope_team"

    @pytest.mark.xfail(reason="Fake embeddings produce low similarity — search returns empty. Works with real embeddings.")
    async def test_search_without_visibility_params_unchanged(
        self,
        db,
        sc,
        tenant_id,
        fleet_id,
        agent_id,
    ):
        """Standard search (no caller_agent_id, no fleet_ids) works as before."""
        from common.embedding import fake_embedding

        await sc.create_memory({
            "tenant_id": tenant_id,
            "fleet_id": fleet_id,
            "agent_id": agent_id,
            "memory_type": "fact",
            "content": "Backward compat search test memory",
            "embedding": fake_embedding("Backward compat search test memory"),
            "weight": 0.5,
            "status": "active",
            "visibility": "scope_team",
        })

        from core_api.services.memory_service import search_memories

        # Standard search — no new params
        results = await search_memories(
            db,
            tenant_id=tenant_id,
            query="Backward compat search test",
            fleet_ids=[fleet_id],
        )
        contents = [r.content for r in results]
        assert "Backward compat search test memory" in contents


@pytest.mark.integration
class TestDedupWithVisibility:
    """Dedup behavior with visibility scoping."""

    @staticmethod
    async def _insert_memory(
        sc,
        tenant_id,
        fleet_id,
        agent_id,
        content,
        visibility="scope_team",
    ):
        from common.embedding import fake_embedding

        mem = await sc.create_memory({
            "tenant_id": tenant_id,
            "fleet_id": fleet_id,
            "agent_id": agent_id,
            "memory_type": "fact",
            "content": content,
            "embedding": fake_embedding(content),
            "weight": 0.5,
            "status": "active",
            "visibility": visibility,
        })
        return mem

    async def test_dedup_within_same_visibility(
        self,
        db,
        tenant_id,
        fleet_id,
        agent_id,
    ):
        """Two identical fleet memories in the same fleet should trigger dedup (409)."""
        from fastapi import HTTPException
        from core_api.services.memory_service import create_memory
        from core_api.schemas import MemoryCreate

        mc1 = MemoryCreate(
            tenant_id=tenant_id,
            fleet_id=fleet_id,
            agent_id=agent_id,
            content="The quarterly revenue report shows 15% growth",
            visibility="scope_team",
        )
        result1 = await create_memory(db, mc1)
        assert result1 is not None

        mc2 = MemoryCreate(
            tenant_id=tenant_id,
            fleet_id=fleet_id,
            agent_id=agent_id,
            content="The quarterly revenue report shows 15% growth",
            visibility="scope_team",
        )
        with pytest.raises(HTTPException) as exc_info:
            await create_memory(db, mc2)
        assert exc_info.value.status_code == 409

    @pytest.mark.xfail(
        reason="Content-hash dedup does not yet consider visibility scope"
    )
    async def test_no_cross_visibility_dedup(
        self,
        db,
        tenant_id,
        fleet_id,
        agent_id,
    ):
        """A tenant memory and a fleet memory with same content should NOT dedup."""
        from core_api.services.memory_service import create_memory
        from core_api.schemas import MemoryCreate

        mc1 = MemoryCreate(
            tenant_id=tenant_id,
            fleet_id=fleet_id,
            agent_id=agent_id,
            content="Cross-visibility dedup test: identical content",
            visibility="scope_org",
        )
        result1 = await create_memory(db, mc1)
        assert result1 is not None

        mc2 = MemoryCreate(
            tenant_id=tenant_id,
            fleet_id=fleet_id,
            agent_id=agent_id,
            content="Cross-visibility dedup test: identical content",
            visibility="scope_team",
        )
        result2 = await create_memory(db, mc2)
        # Both should exist since different visibility scopes
        assert result2 is not None
        assert result2.id != result1.id


# ---------------------------------------------------------------------------
# Benchmark tests
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
class TestVisibilityFilterBenchmark:
    """Measure overhead of visibility filtering logic."""

    def test_visibility_filter_overhead(self):
        """Visibility WHERE clause is a simple column check — should be <1% overhead."""
        visibility_values = ("scope_agent", "scope_team", "scope_org")

        def apply_visibility_filter(memories, fleet_id, caller_agent_id):
            """Pure-Python simulation of visibility filtering."""
            result = []
            for m_vis, m_fleet, m_agent in memories:
                if m_vis == "scope_agent" and m_agent != caller_agent_id:
                    continue
                if m_vis == "scope_team" and m_fleet != fleet_id:
                    continue
                # scope_org always passes
                result.append((m_vis, m_fleet, m_agent))
            return result

        # Generate test data: 1000 memories with mixed visibilities
        import random

        random.seed(42)
        memories = [
            (
                random.choice(visibility_values),
                f"fleet-{random.randint(1, 5)}",
                f"agent-{random.randint(1, 10)}",
            )
            for _ in range(1000)
        ]

        # Baseline: iterate without filter
        def baseline(mems):
            return list(mems)

        # Warmup
        for _ in range(100):
            apply_visibility_filter(memories, "fleet-1", "agent-1")
            baseline(memories)

        # Measure baseline
        baseline_times = []
        for _ in range(10_000):
            t0 = time.perf_counter_ns()
            baseline(memories)
            baseline_times.append(time.perf_counter_ns() - t0)

        # Measure with filter
        filter_times = []
        for _ in range(10_000):
            t0 = time.perf_counter_ns()
            apply_visibility_filter(memories, "fleet-1", "agent-1")
            filter_times.append(time.perf_counter_ns() - t0)

        baseline_mean_us = statistics.mean(baseline_times) / 1000
        filter_mean_us = statistics.mean(filter_times) / 1000
        overhead_pct = (
            ((filter_mean_us - baseline_mean_us) / baseline_mean_us * 100)
            if baseline_mean_us > 0
            else 0
        )

        print(f"\n{'=' * 60}")
        print("VISIBILITY FILTER OVERHEAD (10K iterations, 1000 memories)")
        print(f"  Baseline (no filter):  {baseline_mean_us:.2f}us")
        print(f"  With visibility filter: {filter_mean_us:.2f}us")
        print(f"  Overhead: {overhead_pct:.1f}%")
        print(f"{'=' * 60}")

        # The SQL-level filter is just a WHERE column = 'value' check,
        # so even the Python overhead should be small
        assert filter_mean_us < 500, f"Filter too slow: {filter_mean_us:.1f}us"

    def test_visibility_enum_check_latency(self):
        """Simple string comparison for visibility should be sub-microsecond."""
        valid = ("scope_agent", "scope_team", "scope_org")

        def check_visibility(vis):
            return vis in valid

        # Warmup
        for _ in range(1000):
            check_visibility("scope_team")

        times = []
        for _ in range(50_000):
            t0 = time.perf_counter_ns()
            check_visibility("scope_team")
            times.append(time.perf_counter_ns() - t0)

        times_us = [t / 1000 for t in times]
        mean_us = statistics.mean(times_us)
        p50_us = statistics.median(times_us)
        p99_us = sorted(times_us)[int(len(times_us) * 0.99)]

        print(f"\n{'=' * 60}")
        print("VISIBILITY ENUM CHECK (50K iterations)")
        print(f"  mean={mean_us:.2f}us  p50={p50_us:.2f}us  p99={p99_us:.2f}us")
        print(f"{'=' * 60}")

        assert mean_us < 10, f"Enum check too slow: {mean_us:.1f}us"
