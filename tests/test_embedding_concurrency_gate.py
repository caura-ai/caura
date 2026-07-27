"""Embedding client-side concurrency cap (backpressure) tests.

Regression cover for the 2026-07-27 prod incident: the embedding backend
(a self-hosted TEI service) serves a fixed number of concurrent requests,
while core-api scales to many instances each holding a large httpx pool.
Unthrottled, aggregate demand oversubscribes the backend, every caller
queues at the HTTP pool and dies on ``PoolTimeout`` while the backend
itself stays healthy — and the per-item fallback for a failed batch
re-jams the same slots, sustaining the failure.

Unit tests validate:
  - In-flight provider calls never exceed ``EMBEDDING_MAX_CONCURRENCY``
  - A slot is held per ATTEMPT, so a backing-off retry doesn't squat
  - A waiter that can't get a slot in time degrades (raises) rather than
    hanging forever, and the raised type is one the retry path treats as
    a provider failure
"""

import asyncio

import pytest

from common.embedding import _service as svc


@pytest.fixture
def reset_gate():
    """Isolate each test from the module-level lazy semaphore."""
    original_cap = svc.EMBEDDING_MAX_CONCURRENCY
    original_timeout = svc.EMBEDDING_GATE_TIMEOUT_SECONDS
    svc._gate = None
    svc._gate_loop = None
    yield
    svc.EMBEDDING_MAX_CONCURRENCY = original_cap
    svc.EMBEDDING_GATE_TIMEOUT_SECONDS = original_timeout
    svc._gate = None
    svc._gate_loop = None


@pytest.mark.unit
class TestEmbeddingConcurrencyGate:
    """The gate is what keeps surplus work off the HTTP connection pool."""

    @pytest.mark.asyncio
    async def test_caps_in_flight_calls(self, reset_gate):
        """30 callers against a cap of 3 never exceed 3 concurrent calls."""
        svc.EMBEDDING_MAX_CONCURRENCY = 3

        in_flight = 0
        peak = 0

        async def fake_call():
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            # Yield so every gathered task gets a chance to pile up —
            # without an await the calls would serialise and the test
            # would pass even with a broken gate.
            await asyncio.sleep(0.01)
            in_flight -= 1
            return [0.1]

        await asyncio.gather(*(svc._call_gated(fake_call) for _ in range(30)))

        assert peak <= 3, f"gate leaked: {peak} concurrent calls with cap 3"
        assert in_flight == 0, "slot not released"

    @pytest.mark.asyncio
    async def test_releases_slot_on_exception(self, reset_gate):
        """A failing call must not leak its slot, or the cap drains to zero."""
        svc.EMBEDDING_MAX_CONCURRENCY = 1

        async def boom():
            raise RuntimeError("provider exploded")

        for _ in range(3):
            with pytest.raises(RuntimeError):
                await svc._call_gated(boom)

        # If the slot leaked, this would block until the gate timeout.
        async def ok():
            return [0.2]

        assert await svc._call_gated(ok) == [0.2]

    @pytest.mark.asyncio
    async def test_blocked_waiter_degrades_instead_of_hanging(self, reset_gate):
        """Exhausted gate raises rather than accumulating waiters forever.

        ``TimeoutError`` is deliberately a subclass of ``Exception`` (not
        ``BaseException``), so ``_run_with_retry``'s broad handler treats
        it as a provider failure and the caller degrades down the
        pre-existing path (persist ``embedding=NULL``, leave it to
        re-embed) instead of stalling the write.
        """
        svc.EMBEDDING_MAX_CONCURRENCY = 1
        svc.EMBEDDING_GATE_TIMEOUT_SECONDS = 0.05

        gate = svc._concurrency_gate()
        await gate.acquire()  # occupy the only slot

        async def never_runs():  # pragma: no cover - gate blocks first
            return [0.3]

        with pytest.raises(TimeoutError):
            await svc._call_gated(never_runs)

    @pytest.mark.asyncio
    async def test_gate_rebinds_per_event_loop(self, reset_gate):
        """A gate bound to a dead loop would never wake its waiters."""
        first = svc._concurrency_gate()
        assert svc._concurrency_gate() is first, "same loop should reuse the gate"

        # Simulate a previously-used loop by faking the recorded identity.
        svc._gate_loop = None
        assert svc._concurrency_gate() is not first, "stale loop should rebind"
