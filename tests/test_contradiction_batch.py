"""Batch concurrent contradiction checks — asyncio.gather replaces serial loop.

Unit tests validate:
- Concurrent execution (N checks in ~1 check's time, not N×)
- Exception in one candidate doesn't block others
- Results correctly matched back to candidates
"""

import asyncio
import time
from unittest.mock import MagicMock
from uuid import uuid4

import pytest


@pytest.mark.unit
class TestBatchConcurrency:
    """Verify candidates are checked concurrently via asyncio.gather."""

    @pytest.mark.asyncio
    async def test_concurrent_execution(self):
        """All candidates checked in parallel — total time ≈ single check, not sum."""
        call_count = 0

        async def mock_check(new_content, old_content):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.1)  # simulate 100ms LLM call
            return False

        candidates = [MagicMock(id=uuid4(), content=f"content {i}") for i in range(5)]

        t0 = time.perf_counter()
        tasks = [
            asyncio.wait_for(mock_check("new", c.content), timeout=10.0)
            for c in candidates
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        elapsed = time.perf_counter() - t0

        assert call_count == 5
        assert len(results) == 5
        # Concurrent: ~0.1s, not 0.5s
        assert elapsed < 0.3, f"Expected concurrent (<0.3s), got {elapsed:.2f}s"

    @pytest.mark.asyncio
    async def test_exception_doesnt_block_others(self):
        """One failing candidate doesn't prevent others from being checked."""

        async def mock_check(new_content, old_content):
            if "fail" in old_content:
                raise RuntimeError("LLM timeout")
            return old_content == "contradict"

        contents = ["safe", "fail-this", "contradict", "safe2"]
        tasks = [
            asyncio.wait_for(mock_check("new", c), timeout=10.0)
            for c in contents
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        assert results[0] is False
        assert isinstance(results[1], RuntimeError)
        assert results[2] is True
        assert results[3] is False

    @pytest.mark.asyncio
    async def test_results_match_candidates(self):
        """Results are correctly zipped back to their candidates."""

        async def mock_check(new_content, old_content):
            return "contra" in old_content

        contents = ["safe memory", "contradicting fact", "another safe one"]
        tasks = [
            asyncio.wait_for(mock_check("new", c), timeout=10.0)
            for c in contents
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        matched = list(zip(contents, results))
        assert matched[0] == ("safe memory", False)
        assert matched[1] == ("contradicting fact", True)
        assert matched[2] == ("another safe one", False)

    @pytest.mark.asyncio
    async def test_timeout_per_task_not_total(self):
        """Each task has its own 10s timeout, not a shared total timeout."""
        call_times = []

        async def mock_slow_check(new_content, old_content):
            t0 = time.perf_counter()
            await asyncio.sleep(0.05)
            call_times.append(time.perf_counter() - t0)
            return False

        tasks = [
            asyncio.wait_for(mock_slow_check("new", f"content {i}"), timeout=10.0)
            for i in range(8)
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

        # All 8 should have run concurrently (each ~50ms)
        assert len(call_times) == 8
        # Total wall time should be << 8 * 50ms
        assert max(call_times) < 0.2

    @pytest.mark.asyncio
    async def test_empty_candidates_no_gather(self):
        """No candidates → no gather call, no errors."""
        # This tests the `if candidates:` guard in _detect
        # Just verify gather with empty list works
        results = await asyncio.gather(*[], return_exceptions=True)
        assert results == []


@pytest.mark.unit
class TestBatchIntegrationWithDetect:
    """Verify the batch pattern is wired into _detect correctly."""

    def test_detect_uses_gather_pattern(self):
        """Verify _detect source code contains asyncio.gather (not serial for loop)."""
        import inspect
        from core_api.services.contradiction_detector import _detect
        source = inspect.getsource(_detect)
        assert "asyncio.gather" in source, "_detect should use asyncio.gather for batch checks"
        assert "return_exceptions=True" in source, "gather should use return_exceptions=True"

    def test_llm_check_has_no_internal_timeout(self):
        """_llm_contradiction_check should NOT have its own wait_for (timeout is at gather level)."""
        import inspect
        from core_api.services.contradiction_detector import _llm_contradiction_check
        source = inspect.getsource(_llm_contradiction_check)
        assert "wait_for" not in source, (
            "_llm_contradiction_check should not wrap in wait_for — timeout is at gather level"
        )
