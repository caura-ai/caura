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
    hanging forever
  - That timeout is classified as CAPACITY, not provider failure: the retry
    path declines to retry it (retrying saturation deepens it) and it does
    not advance the provider's degraded streak
"""

import asyncio

import pytest

from common.embedding import _service as svc


@pytest.fixture
def reset_gate():
    """Isolate each test from the module-level lazy semaphores."""
    original_cap = svc.EMBEDDING_MAX_CONCURRENCY
    original_timeout = svc.EMBEDDING_GATE_TIMEOUT_SECONDS
    original_bg_cap = svc.EMBEDDING_BACKGROUND_MAX_CONCURRENCY
    svc._gate = None
    svc._gate_loop = None
    svc._bg_gate = None
    svc._bg_gate_loop = None
    yield
    svc.EMBEDDING_MAX_CONCURRENCY = original_cap
    svc.EMBEDDING_GATE_TIMEOUT_SECONDS = original_timeout
    svc.EMBEDDING_BACKGROUND_MAX_CONCURRENCY = original_bg_cap
    svc._gate = None
    svc._gate_loop = None
    svc._bg_gate = None
    svc._bg_gate_loop = None


def _set_caps(total: int, reserved: int) -> None:
    """Point the module at a (total, reserved) split for one test.

    Sets only what the gate reads — the shared cap and the derived
    background cap. ``reserved`` is expressed here as the difference so a
    test can't configure an inconsistent triple.
    """
    svc.EMBEDDING_MAX_CONCURRENCY = total
    svc.EMBEDDING_BACKGROUND_MAX_CONCURRENCY = total - reserved
    svc._gate = None
    svc._gate_loop = None
    svc._bg_gate = None
    svc._bg_gate_loop = None


@pytest.mark.unit
class TestEmbeddingConcurrencyGate:
    """The gate is what keeps surplus work off the HTTP connection pool."""

    @pytest.mark.asyncio
    async def test_caps_in_flight_calls(self, reset_gate):
        """30 callers against a cap of 3 never exceed 3 concurrent calls."""
        _set_caps(total=3, reserved=0)

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

        await asyncio.gather(*(svc.call_embedding_gated(fake_call, background=False) for _ in range(30)))

        assert peak <= 3, f"gate leaked: {peak} concurrent calls with cap 3"
        assert in_flight == 0, "slot not released"

    @pytest.mark.asyncio
    async def test_releases_slot_on_exception(self, reset_gate):
        """A failing call must not leak its slot, or the cap drains to zero."""
        _set_caps(total=1, reserved=0)

        async def boom():
            raise RuntimeError("provider exploded")

        for _ in range(3):
            with pytest.raises(RuntimeError):
                await svc.call_embedding_gated(boom, background=False)

        # If the slot leaked, this would block until the gate timeout.
        async def ok():
            return [0.2]

        assert await svc.call_embedding_gated(ok, background=False) == [0.2]

    @pytest.mark.asyncio
    async def test_blocked_waiter_degrades_instead_of_hanging(self, reset_gate):
        """Exhausted gate raises rather than accumulating waiters forever.

        Raises :class:`EmbeddingGateTimeout` — a ``TimeoutError`` subclass,
        so it stays an ``Exception`` (not ``BaseException``) and the caller
        still degrades down the pre-existing path (persist
        ``embedding=NULL``, leave it to re-embed) instead of stalling the
        write. The distinct type is what lets ``_run_with_retry`` decline to
        retry it; see ``TestGateTimeoutIsNotRetried``.
        """
        _set_caps(total=1, reserved=0)
        svc.EMBEDDING_GATE_TIMEOUT_SECONDS = 0.05

        gate = svc._concurrency_gate()
        await gate.acquire()  # occupy the only slot

        async def never_runs():  # pragma: no cover - gate blocks first
            return [0.3]

        with pytest.raises(TimeoutError):
            await svc.call_embedding_gated(never_runs, background=False)

    @pytest.mark.asyncio
    async def test_gate_rebinds_per_event_loop(self, reset_gate):
        """A gate bound to a dead loop would never wake its waiters."""
        first = svc._concurrency_gate()
        assert svc._concurrency_gate() is first, "same loop should reuse the gate"

        # Simulate a previously-used loop by faking the recorded identity.
        svc._gate_loop = None
        assert svc._concurrency_gate() is not first, "stale loop should rebind"


@pytest.mark.unit
class TestQueryEmbeddingReservation:
    """Reserved slots keep live recall answerable during a write flood.

    Regression cover for 2026-08-18: a bulk write burst (~1.3 k memories in
    2 h against a ~5/h baseline) held every slot of the priority-blind cap
    and produced 118 ``Query embedding failed after 2 attempts`` errors —
    user-visible search degradation — while the backend itself stayed
    healthy at ~3 ms inference with spare container concurrency.
    """

    @pytest.mark.asyncio
    async def test_query_embed_still_runs_while_background_saturated(self, reset_gate):
        """THE guarantee: background work cannot starve a query embed.

        The flood is sized to the FULL shared cap, not to the background
        budget — that distinction is what makes this test meaningful.
        Without the reservation all ``total`` background calls occupy every
        shared slot and the query embed times out (verified: this test fails
        against a priority-blind gate). With it, background is held to
        ``total - reserved``, so the reserved slots stay acquirable.
        """
        total, reserved = 4, 2
        _set_caps(total=total, reserved=reserved)  # background budget = 2
        svc.EMBEDDING_GATE_TIMEOUT_SECONDS = 0.5

        parked = asyncio.Event()
        background_running = asyncio.Semaphore(0)

        async def background_call():
            background_running.release()
            await parked.wait()
            return [0.1]

        # ``total`` of them: enough to hold every shared slot if nothing
        # stopped background from doing so.
        bg_tasks = [
            asyncio.create_task(svc.call_embedding_gated(background_call, background=True))
            for _ in range(total)
        ]
        # Wait until the background budget is genuinely occupied and parked.
        for _ in range(total - reserved):
            await asyncio.wait_for(background_running.acquire(), timeout=1)

        async def query_call():
            return [0.2]

        # No timeout suppression: without the reservation this raises
        # TimeoutError and the test fails.
        assert await svc.call_embedding_gated(query_call, background=False) == [0.2]

        parked.set()
        assert await asyncio.gather(*bg_tasks) == [[0.1]] * total

    @pytest.mark.asyncio
    async def test_background_capped_below_total(self, reset_gate):
        """Background in-flight never exceeds ``total - reserved``."""
        _set_caps(total=5, reserved=2)  # background budget = 3

        in_flight = 0
        peak = 0

        async def call():
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0)
            in_flight -= 1
            return [0.0]

        await asyncio.gather(
            *(svc.call_embedding_gated(call, background=True) for _ in range(40))
        )
        assert peak <= 3, f"background peaked at {peak}, budget is 3"

    @pytest.mark.asyncio
    async def test_total_cap_still_holds_across_both_classes(self, reset_gate):
        """The backend-protection invariant survives the split.

        Reserving slots must not let (background + query) exceed the shared
        cap — that cap is what keeps surplus work off the backend, and the
        reservation is a partition of it, not an addition to it.
        """
        _set_caps(total=4, reserved=2)

        in_flight = 0
        peak = 0

        async def call():
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0)
            in_flight -= 1
            return [0.0]

        await asyncio.gather(
            *(
                svc.call_embedding_gated(call, background=bool(i % 2))
                for i in range(60)
            )
        )
        assert peak <= 4, f"total peaked at {peak}, shared cap is 4"

    @pytest.mark.asyncio
    async def test_background_slot_released_when_shared_gate_times_out(self, reset_gate):
        """A background waiter that gives up must not leak its slot.

        Background takes the background slot first, then the shared one. If
        the second acquire times out and the first is not released, the
        background budget erodes by one per abandoned wait until writes stop
        entirely — a slow strangulation that would look like the very
        saturation this change fixes.
        """
        _set_caps(total=2, reserved=1)  # background budget = 1
        svc.EMBEDDING_GATE_TIMEOUT_SECONDS = 0.05

        shared = svc._concurrency_gate()
        await shared.acquire()
        await shared.acquire()  # shared gate fully held; background cannot proceed

        async def never_runs():  # pragma: no cover - gate blocks first
            return [0.3]

        with pytest.raises(TimeoutError):
            await svc.call_embedding_gated(never_runs, background=True)

        # The abandoned attempt must have handed its background slot back.
        assert not svc._background_gate().locked(), "background slot leaked on timeout"

        shared.release()
        shared.release()

        async def ok():
            return [0.4]

        assert await svc.call_embedding_gated(ok, background=True) == [0.4]

    @pytest.mark.asyncio
    async def test_get_embedding_honours_background_false(self, reset_gate):
        """``get_embedding(background=False)`` must reach the reserved slots.

        ``get_embedding`` defaults to the background budget, but document
        search (``POST /documents/search``, ``caura_doc op=search``) embeds
        its query through it and raises 503 on ``None``. Left on the
        background budget those searches would be throttled behind the write
        floods this reservation exists to survive. Pins the opt-out so the
        default can't silently re-capture them.
        """
        total, reserved = 4, 2
        _set_caps(total=total, reserved=reserved)
        svc.EMBEDDING_GATE_TIMEOUT_SECONDS = 0.5

        parked = asyncio.Event()
        running = asyncio.Semaphore(0)

        async def background_call():
            running.release()
            await parked.wait()
            return [0.1]

        bg_tasks = [
            asyncio.create_task(svc.call_embedding_gated(background_call, background=True))
            for _ in range(total)
        ]
        for _ in range(total - reserved):
            await asyncio.wait_for(running.acquire(), timeout=1)

        class _Provider:
            async def embed(self, text: str) -> list[float]:
                return [0.9]

        monkey = _Provider()

        async def _fake_resolve(tenant_config, context):
            return monkey

        original = svc._resolve_provider_or_degrade
        svc._resolve_provider_or_degrade = _fake_resolve
        try:
            # Interactive: must succeed against a saturated background budget.
            assert await svc.get_embedding("q", background=False) == [0.9]
        finally:
            svc._resolve_provider_or_degrade = original

        parked.set()
        await asyncio.gather(*bg_tasks)

    @pytest.mark.asyncio
    async def test_no_reservation_lets_background_use_full_cap(self, reset_gate):
        """At ``reserved=0`` background can still use the whole cap.

        The background gate is always interposed (simpler than an Optional
        through every release path), so this pins that sizing it equal to the
        shared cap costs no throughput — only one uncontended extra acquire.
        Reachable in practice only at ``cap == 1``; see the clamp note in
        ``constants.py``.
        """
        _set_caps(total=3, reserved=0)

        in_flight = 0
        peak = 0

        async def call():
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0)
            in_flight -= 1
            return [0.0]

        await asyncio.gather(
            *(svc.call_embedding_gated(call, background=True) for _ in range(30))
        )
        assert peak == 3, f"background should reach the full cap of 3, got {peak}"


@pytest.mark.unit
class TestGateTimeoutIsNotRetried:
    """A gate timeout is a capacity signal, so the retry path must decline it.

    Retrying saturation deepens it: every attempt queues another waiter for
    another full ``EMBEDDING_GATE_TIMEOUT_SECONDS``, so the retry adds load
    to the backlog it is reacting to and the caller waits
    ``attempts x timeout + backoff`` to learn what attempt one already knew.
    """

    @staticmethod
    def _counting(exc: BaseException):
        """A ``call_embedding_gated`` stand-in that records attempts and always raises."""
        calls = {"n": 0}

        async def fake_gated(make_call, *, background):
            calls["n"] += 1
            raise exc

        return calls, fake_gated

    @pytest.mark.asyncio
    async def test_gate_timeout_attempted_once(self, monkeypatch, reset_gate):
        calls, fake = self._counting(svc.EmbeddingGateTimeout("saturated"))
        monkeypatch.setattr(svc, "call_embedding_gated", fake)
        stats = svc._EmbeddingStats(label="test-backend")

        result = await svc._run_with_retry(
            lambda: None, "Embedding", stats, background=True
        )

        assert result is None, "caller must still degrade to None"
        assert calls["n"] == 1, f"gate timeout was retried {calls['n']} times"

    @pytest.mark.asyncio
    async def test_provider_error_is_still_retried(self, monkeypatch, reset_gate):
        """The narrowing must not disarm retries for real provider faults."""
        calls, fake = self._counting(RuntimeError("backend exploded"))
        monkeypatch.setattr(svc, "call_embedding_gated", fake)
        monkeypatch.setattr(svc, "EMBEDDING_RETRY_DELAY_S", 0)
        stats = svc._EmbeddingStats(label="test-backend")

        result = await svc._run_with_retry(
            lambda: None, "Embedding", stats, background=True
        )

        assert result is None
        assert calls["n"] == svc.EMBEDDING_RETRY_ATTEMPTS, (
            f"provider error should be retried {svc.EMBEDDING_RETRY_ATTEMPTS}x, "
            f"got {calls['n']}"
        )

    @pytest.mark.asyncio
    async def test_gate_timeout_does_not_blame_the_provider(
        self, monkeypatch, reset_gate
    ):
        """It must not advance the streak behind "Embedding service degraded".

        Our own queue is not evidence about the backend's health, and that
        streak is what an operator reads to decide the backend is unhealthy.
        """
        stats = svc._EmbeddingStats(label="test-backend")

        _, gate_timeout = self._counting(svc.EmbeddingGateTimeout("saturated"))
        monkeypatch.setattr(svc, "call_embedding_gated", gate_timeout)
        for _ in range(5):
            await svc._run_with_retry(lambda: None, "Embedding", stats, background=True)
        assert stats.consecutive_failures == 0, (
            "gate timeouts advanced the provider-degraded streak "
            f"to {stats.consecutive_failures}"
        )

        # Control: a genuine provider fault still does advance it, so the
        # assertion above is about classification and not a dead counter.
        _, provider_error = self._counting(RuntimeError("backend exploded"))
        monkeypatch.setattr(svc, "call_embedding_gated", provider_error)
        monkeypatch.setattr(svc, "EMBEDDING_RETRY_DELAY_S", 0)
        await svc._run_with_retry(lambda: None, "Embedding", stats, background=True)
        assert stats.consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_earlier_provider_failure_survives_a_later_gate_timeout(
        self, monkeypatch, reset_gate
    ):
        """A real failure then a gate timeout must still count the real one.

        ``record_failure`` is otherwise only reached by exhausting the loop,
        so returning early on the timeout would discard evidence the backend
        had already misbehaved on an earlier attempt of the SAME call. That
        mix — erroring and slow enough to saturate the gate — is what an
        outage looks like, which is exactly when the degraded signal must not
        go quiet.
        """
        monkeypatch.setattr(svc, "EMBEDDING_RETRY_DELAY_S", 0)
        stats = svc._EmbeddingStats(label="test-backend")
        calls = {"n": 0}

        async def provider_error_then_gate_timeout(make_call, *, background):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("backend exploded")
            raise svc.EmbeddingGateTimeout("saturated")

        monkeypatch.setattr(
            svc, "call_embedding_gated", provider_error_then_gate_timeout
        )

        result = await svc._run_with_retry(
            lambda: None, "Embedding", stats, background=True
        )

        assert result is None
        assert calls["n"] == 2, "attempt 1 failed for a real reason; it should retry"
        assert stats.consecutive_failures == 1, (
            "the real provider failure from attempt 1 was dropped when attempt 2 "
            "hit the gate"
        )
