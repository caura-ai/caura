"""Batch concurrent contradiction checks — asyncio.gather replaces serial loop.

Unit tests validate:
- Concurrent execution (all N checks in flight at once, not one at a time)
- Exception in one candidate doesn't block others
- Results correctly matched back to candidates

Concurrency is asserted as a property — the peak number of checks in flight —
never as a wall-clock budget. A duration threshold turns the event loop's
scheduling luck into a pass/fail verdict, which is a flake on a shared runner,
not a signal about the code.
"""

import asyncio
from unittest.mock import MagicMock
from uuid import uuid4

import pytest


@pytest.mark.unit
class TestBatchConcurrency:
    """Verify candidates are checked concurrently via asyncio.gather."""

    @pytest.mark.asyncio
    async def test_concurrent_execution(self):
        """All candidates are in flight at once, asserted by peak overlap.

        The property this test exists for is OVERLAP — every check is started
        before any of them finishes. Asserting a wall-clock budget instead
        (``elapsed < 0.3`` for five 100ms sleeps) measured overlap only
        indirectly, and left the verdict to the scheduler: the margin was
        0.2s of absolute headroom, which a loaded CI runner can eat without
        anything being wrong with the code. The in-flight peak is the same
        claim stated directly, and it is decided by the event loop's ordering
        rather than by the clock — see ``test_contradiction_fanout_bounded``
        for the same idiom.
        """
        call_count = 0
        in_flight = 0
        peak_in_flight = 0

        async def mock_check(new_content, old_content):
            nonlocal call_count, in_flight, peak_in_flight
            call_count += 1
            in_flight += 1
            peak_in_flight = max(peak_in_flight, in_flight)
            try:
                await asyncio.sleep(0.005)  # stand-in for the LLM round trip
                return False
            finally:
                in_flight -= 1

        candidates = [MagicMock(id=uuid4(), content=f"content {i}") for i in range(5)]

        tasks = [
            asyncio.wait_for(mock_check("new", c.content), timeout=10.0)
            for c in candidates
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        assert call_count == 5
        assert len(results) == 5
        # Serial execution would peak at 1: each check would have to finish
        # before the next one starts.
        assert peak_in_flight == 5, (
            f"checks did not overlap — peak in flight was {peak_in_flight}, expected 5"
        )

    @pytest.mark.asyncio
    async def test_exception_doesnt_block_others(self):
        """One failing candidate doesn't prevent others from being checked."""

        async def mock_check(new_content, old_content):
            if "fail" in old_content:
                raise RuntimeError("LLM timeout")
            return old_content == "contradict"

        contents = ["safe", "fail-this", "contradict", "safe2"]
        tasks = [asyncio.wait_for(mock_check("new", c), timeout=10.0) for c in contents]
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
        tasks = [asyncio.wait_for(mock_check("new", c), timeout=10.0) for c in contents]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        matched = list(zip(contents, results))
        assert matched[0] == ("safe memory", False)
        assert matched[1] == ("contradicting fact", True)
        assert matched[2] == ("another safe one", False)

    @pytest.mark.asyncio
    async def test_timeout_per_task_not_total(self):
        """Each task gets its own timeout, so none is starved by the others.

        Same fix as ``test_concurrent_execution``, and the old assertion here
        was additionally measuring the wrong thing: it timed each call's OWN
        duration (``t0`` was taken inside ``mock_slow_check``) and asserted
        ``max(call_times) < 0.2``. A serial loop produces exactly the same
        ~50ms per-call numbers, so the check could not have caught the
        regression its docstring describes — it could only flake. What the
        per-task ``wait_for`` actually buys is that all 8 run under their own
        budget concurrently and none is cut off, which is what is asserted now.
        """
        completed = []
        in_flight = 0
        peak_in_flight = 0

        async def mock_slow_check(new_content, old_content):
            nonlocal in_flight, peak_in_flight
            in_flight += 1
            peak_in_flight = max(peak_in_flight, in_flight)
            try:
                await asyncio.sleep(0.005)
                completed.append(old_content)
                return False
            finally:
                in_flight -= 1

        tasks = [
            asyncio.wait_for(mock_slow_check("new", f"content {i}"), timeout=10.0)
            for i in range(8)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        assert len(completed) == 8
        # A shared total budget would cut the tail off; a per-task budget does not.
        assert not any(isinstance(r, asyncio.TimeoutError) for r in results)
        assert peak_in_flight == 8, (
            f"checks did not overlap — peak in flight was {peak_in_flight}, expected 8"
        )

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

    def test_detect_uses_batched_judge(self):
        """A61: _detect judges MULTIPLE candidates via ONE batched LLM call
        (``_llm_contradiction_check_batch``) rather than a per-candidate
        ``asyncio.gather`` fan-out (the prod OpenAI cost driver). A single
        candidate keeps the direct per-candidate call."""
        import inspect

        from core_api.services.contradiction_detector import _detect

        source = inspect.getsource(_detect)
        assert "_llm_contradiction_check_batch" in source, (
            "_detect should batch multi-candidate judging into one LLM call"
        )
        assert "len(candidates) == 1" in source, (
            "_detect should keep the direct per-candidate call for a single candidate"
        )

    def test_llm_check_has_no_internal_timeout(self):
        """_llm_contradiction_check should NOT have its own wait_for (timeout is at gather level)."""
        import inspect

        from core_api.services.contradiction_detector import _llm_contradiction_check

        source = inspect.getsource(_llm_contradiction_check)
        assert "wait_for" not in source, (
            "_llm_contradiction_check should not wrap in wait_for — timeout is at gather level"
        )


@pytest.mark.unit
class TestBatchedJudgeA61:
    """A61 — the single-call batched judge."""

    @pytest.mark.asyncio
    async def test_batch_aligns_and_defaults_missing(self):
        """One complete_json call; raws aligned to input order; a missing/
        malformed entry defaults to a safe non-contradiction."""
        from unittest.mock import AsyncMock, patch

        from core_api.services.contradiction_detector import (
            _llm_contradiction_check_batch,
        )

        cands = [{"content": "a"}, {"content": "b"}, {"content": "c"}]
        # model returns index 0 + 2 only; index "1" missing; extra junk key
        model = {
            "0": {"contradicts": True, "relationship": "negation"},
            "2": {"contradicts": False},
            "99": {"contradicts": True},
        }
        fake_llm = AsyncMock()
        fake_llm.complete_json = AsyncMock(return_value=model)
        calls = {"n": 0}

        async def _one_call(**kw):
            calls["n"] += 1
            return await kw["call_fn"](fake_llm)

        with patch(
            "core_api.services.contradiction_detector.call_with_fallback",
            side_effect=_one_call,
        ):
            raws = await _llm_contradiction_check_batch("new", cands)

        assert calls["n"] == 1  # ONE LLM call for all candidates
        assert len(raws) == 3  # aligned to input order
        assert raws[0]["contradicts"] is True
        assert raws[1] == {"contradicts": False}  # missing -> safe default
        assert raws[2]["contradicts"] is False

    @pytest.mark.asyncio
    async def test_detect_multi_candidate_calls_batch_once(self):
        """_detect with 3 semantic candidates makes ONE batched judge call
        (not 3), and still applies the supersede effect."""
        from unittest.mock import AsyncMock, patch
        from uuid import uuid4

        from core_api.constants import VECTOR_DIM
        from core_api.services.contradiction_detector import _detect
        from tests._contradiction_batch_compat import install_batch_status_replay_shim

        new = {
            "id": str(uuid4()),
            "tenant_id": "t1",
            "fleet_id": "f1",
            "content": "Dan lives in Haifa",
            "subject_entity_id": None,
            "predicate": None,
            "object_value": None,
            "deleted_at": None,
            "status": "active",
            "visibility": "scope_team",
            "supersedes_id": None,
            "created_at": "2026-04-29T12:00:00+00:00",
        }
        cands = [
            {
                "id": str(uuid4()),
                "content": f"Dan lives in city {i}",
                "status": "active",
                "created_at": "2026-04-29T10:00:00+00:00",
            }
            for i in range(3)
        ]
        sc = AsyncMock()
        sc.find_rdf_conflicts = AsyncMock(return_value=[])
        sc.find_similar_candidates = AsyncMock(return_value=cands)
        sc.update_memory_status = AsyncMock()
        install_batch_status_replay_shim(sc)

        batch = AsyncMock(
            return_value=[{"same_subject": True, "contradicts": True} for _ in cands]
        )
        with (
            patch(
                "core_api.services.contradiction_detector.get_storage_client",
                return_value=sc,
            ),
            patch(
                "core_api.services.contradiction_detector._llm_contradiction_check_batch",
                batch,
            ),
        ):
            await _detect(new, [0.1] * VECTOR_DIM)

        batch.assert_awaited_once()  # ONE batched call for 3 candidates
        # effect applied: at least one candidate marked conflicted
        statuses = [c.args for c in sc.update_memory_status.call_args_list]
        assert any(s[1] == "conflicted" for s in statuses)
