"""Memory redistribution schema tests.

Unit tests validate:
- Schema validation (min/max memory_ids, target_agent_id required)
- RedistributeResponse model fields

The route, its trust gates and the move itself are tested over HTTP in
``tests/test_route_authz_gaps.py`` (L-172). The integration tests that were
here re-implemented the move inline and asserted their own assignments, so
none of them could fail whatever the route did.
"""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from core_api.schemas import RedistributeRequest, RedistributeResponse

# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRedistributeSchemas:
    """Validate request/response schemas."""

    def test_request_requires_memory_ids(self):
        with pytest.raises(ValidationError):
            RedistributeRequest(memory_ids=[], target_agent_id="agent-b")

    def test_request_requires_target_agent_id(self):
        with pytest.raises(ValidationError):
            RedistributeRequest(memory_ids=[uuid4()], target_agent_id="")

    def test_request_accepts_valid_input(self):
        req = RedistributeRequest(
            memory_ids=[uuid4(), uuid4()],
            target_agent_id="security-agent",
        )
        assert len(req.memory_ids) == 2
        assert req.target_agent_id == "security-agent"

    def test_request_max_500_memory_ids(self):
        with pytest.raises(ValidationError):
            RedistributeRequest(
                memory_ids=[uuid4() for _ in range(501)],
                target_agent_id="agent-b",
            )

    def test_request_accepts_500_memory_ids(self):
        req = RedistributeRequest(
            memory_ids=[uuid4() for _ in range(500)],
            target_agent_id="agent-b",
        )
        assert len(req.memory_ids) == 500

    def test_response_model_fields(self):
        resp = RedistributeResponse(
            moved=10,
            promoted=2,
            skipped=1,
            errors=[],
            redistribute_ms=42,
        )
        assert resp.moved == 10
        assert resp.promoted == 2
        assert resp.skipped == 1
        assert resp.redistribute_ms == 42


# ---------------------------------------------------------------------------
# Benchmark tests
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
class TestRedistributeBenchmark:
    """Measure redistribution overhead."""

    def test_schema_validation_latency(self):
        """Schema validation for 500 UUIDs should be fast."""
        import time

        ids = [uuid4() for _ in range(500)]

        t0 = time.perf_counter_ns()
        for _ in range(100):
            RedistributeRequest(memory_ids=ids, target_agent_id="target")
        elapsed_us = (time.perf_counter_ns() - t0) / 1000 / 100

        print(f"\n  Schema validation (500 ids): {elapsed_us:.1f}μs")
        assert elapsed_us < 10_000, f"Too slow: {elapsed_us:.0f}μs"
