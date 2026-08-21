"""A declared budget must be enforced, not merely documented.

Before this, the worst case of an LLM call was enforced by parameter
VALUES. ``recall`` spells the arithmetic out — "one primary attempt (15s),
then one fallback attempt (15s) ... worst case ~30s" — but the guarantee
held only while ``max_attempts`` stayed 1. Restoring the default, which
looks entirely reasonable, silently doubled it, with a comment as the only
thing saying otherwise. Same class as a flag whose default degrades
correctness: the property lived in a value rather than in the structure.

``budget_s`` moves it into the structure. Three things it fixes, each
tested below:

  1. The loop stops when the budget is spent, instead of starting an
     attempt that cannot finish.
  2. Each attempt's own timeout is clamped to what is left, so the budget
     binds the attempt too rather than being advisory — enforced only by
     whoever cancels from outside.
  3. The ``Retry-After`` cap is DERIVED from the remaining budget instead
     of read from ``LLM_MAX_RETRY_AFTER_S``, which exists for the callers
     that declare nothing.

Absent a budget, every one of these paths must behave exactly as before —
14 of the 16 ``call_with_fallback`` sites declare no timeout at all, so
that is the common case and the controls here cover it.
"""

from __future__ import annotations

import asyncio

import httpx
import openai
import pytest

from common.llm import retry as retry_mod
from common.llm.retry import call_with_fallback, call_with_retry


def _rate_limited(**headers: str) -> openai.RateLimitError:
    response = httpx.Response(
        429,
        headers=headers,
        request=httpx.Request("POST", "http://provider/v1/chat/completions"),
    )
    return openai.RateLimitError("slow down", response=response, body=None)


@pytest.fixture
def clock(monkeypatch):
    """A monotonic clock that only moves when a sleep is taken.

    Real time would make every assertion here a race. Advancing the clock
    by exactly the sleep that was requested keeps the budget arithmetic
    deterministic and lets a test spend a budget without waiting for it.
    """

    class _Clock:
        def __init__(self) -> None:
            self.now = 1_000.0
            self.slept: list[float] = []

        def monotonic(self) -> float:
            return self.now

        async def sleep(self, seconds: float) -> None:
            self.slept.append(seconds)
            self.now += seconds

        def advance(self, seconds: float) -> None:
            self.now += seconds

    c = _Clock()
    monkeypatch.setattr(retry_mod.time, "monotonic", c.monotonic)
    monkeypatch.setattr(retry_mod.asyncio, "sleep", c.sleep)
    monkeypatch.setattr(retry_mod, "LLM_RETRY_JITTER_FRACTION", 0.0)
    return c


@pytest.mark.unit
class TestTheBudgetBoundsTheLoop:
    @pytest.mark.asyncio
    async def test_no_attempt_starts_once_the_budget_is_spent(self, clock):
        """An attempt with no time left is a wasted request, not a retry."""
        calls = 0

        async def _burn():
            nonlocal calls
            calls += 1
            # Each attempt consumes 4s of a 10s budget.
            clock.advance(4.0)
            raise _rate_limited()

        with pytest.raises(openai.RateLimitError):
            await call_with_retry(
                _burn, label="t", max_attempts=5, base_delay=1.0, budget_s=10.0
            )

        # 4s + 1s sleep + 4s = 9s, leaving 1s; the third attempt would
        # start with less than its own backoff already spent, so the wait
        # check stops it. Without a budget all five would have run.
        assert calls == 2

    @pytest.mark.asyncio
    async def test_without_a_budget_every_attempt_runs(self, clock):
        """The control. This is the shape 14 of 16 call sites are in."""
        calls = 0

        async def _burn():
            nonlocal calls
            calls += 1
            clock.advance(4.0)
            raise _rate_limited()

        with pytest.raises(openai.RateLimitError):
            await call_with_retry(_burn, label="t", max_attempts=5, base_delay=1.0)

        assert calls == 5

    @pytest.mark.asyncio
    async def test_a_wait_that_would_spend_the_rest_is_not_taken(self, clock):
        """Sleeping the budget away and then giving up is the worst outcome.

        Only the loop-top check would catch this, and only after the sleep
        had already burned the time it was checking for.
        """

        async def _slow():
            clock.advance(2.5)
            raise _rate_limited()

        with pytest.raises(openai.RateLimitError):
            await call_with_retry(
                _slow, label="t", max_attempts=3, base_delay=1.0, budget_s=3.0
            )

        assert clock.slept == []

    @pytest.mark.asyncio
    async def test_the_promise_survives_restoring_the_default_attempts(self, clock):
        """The reason this parameter exists, stated as a test.

        ``recall``'s "worst case ~30s" was enforced by ``max_attempts=1``
        being the value it is. This runs the same call with the DEFAULT 2
        attempts — the change that used to double it — and shows the wall
        clock still cannot exceed the declared 15s. Without a budget the
        same call takes 15 + 1 + 15 = 31s.
        """
        started = clock.now

        async def _slow():
            clock.advance(15.0)  # an attempt that uses its whole timeout
            raise _rate_limited()

        with pytest.raises(openai.RateLimitError):
            await call_with_retry(
                _slow,
                label="recall-primary",
                max_attempts=2,
                base_delay=1.0,
                timeout=15.0,
                budget_s=15.0,
            )

        assert clock.now - started <= 15.0
        assert clock.slept == []

    @pytest.mark.asyncio
    async def test_and_without_a_budget_it_does_not(self, clock):
        """The fragility itself, so the test above is not vacuous."""
        started = clock.now

        async def _slow():
            clock.advance(15.0)
            raise _rate_limited()

        with pytest.raises(openai.RateLimitError):
            await call_with_retry(
                _slow,
                label="recall-primary",
                max_attempts=2,
                base_delay=1.0,
                timeout=15.0,
            )

        assert clock.now - started == pytest.approx(31.0)

    @pytest.mark.asyncio
    async def test_a_budget_gone_before_the_first_attempt_still_raises_usefully(
        self, monkeypatch
    ):
        """The one break that can be reached with nothing caught yet.

        The other two live inside the ``except``, so ``last_exc`` is always
        set there. This one can fire on the first iteration, and would then
        reach ``raise last_exc`` with ``None`` — reported by Python as
        "exceptions must derive from BaseException", a type error standing
        in for a perfectly clear condition.

        Driven by a clock that jumps between the deadline being computed and
        the check reading it, rather than by a tiny budget: a real
        sub-microsecond budget makes this a race, and the point is the
        control flow, not the timing.
        """
        ticks = iter([0.0, 100.0, 200.0, 300.0])
        monkeypatch.setattr(retry_mod.time, "monotonic", lambda: next(ticks))
        calls = 0

        async def _never_runs():
            nonlocal calls
            calls += 1
            return "should not get here"

        with pytest.raises(TimeoutError, match="budget was spent"):
            await call_with_retry(_never_runs, label="t", budget_s=1.0)

        assert calls == 0

    @pytest.mark.asyncio
    async def test_a_real_failure_outranks_the_synthetic_timeout(self, clock):
        """Running out of time must not overwrite a provider's own error.

        Same precedence the rest of the loop applies: a genuine failure is
        more useful to the caller than "we ran out of time", so the
        synthetic exception is only for when nothing was caught at all.
        """

        async def _fails_then_time_runs_out():
            clock.advance(10.0)
            raise _rate_limited()

        with pytest.raises(openai.RateLimitError):
            await call_with_retry(
                _fails_then_time_runs_out,
                label="t",
                max_attempts=3,
                base_delay=0.1,
                budget_s=10.0,
            )

    @pytest.mark.asyncio
    async def test_a_non_positive_budget_is_a_misconfiguration(self):
        """Fail loudly rather than silently running one unbounded attempt."""
        for bad in (0.0, -1.0):
            with pytest.raises(ValueError, match="budget_s must be > 0"):
                await call_with_retry(lambda: asyncio.sleep(0), label="t", budget_s=bad)


@pytest.mark.unit
class TestTheBudgetBoundsEachAttempt:
    @pytest.mark.asyncio
    async def test_an_attempt_cannot_outlive_what_is_left(self, clock):
        """Otherwise the budget is advisory — enforced only from outside.

        10s declared; the first attempt burns 8s and the backoff 0.5s, so
        the second must be granted the 1.5s that is left, not the 15s its
        own per-attempt timeout allows.
        """
        granted: list[float | None] = []
        real_wait_for = asyncio.wait_for

        async def _spy(coro, timeout):
            granted.append(timeout)
            return await real_wait_for(coro, timeout=timeout)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(retry_mod.asyncio, "wait_for", _spy)

            async def _burn():
                clock.advance(8.0)
                raise _rate_limited()

            with pytest.raises(openai.RateLimitError):
                await call_with_retry(
                    _burn,
                    label="t",
                    max_attempts=2,
                    base_delay=0.5,
                    timeout=15.0,
                    budget_s=10.0,
                )

        assert granted[0] == 10.0  # min(timeout=15, remaining=10)
        assert granted[1] == pytest.approx(1.5)  # 10 - 8 spent - 0.5 slept

    @pytest.mark.asyncio
    async def test_a_budget_alone_bounds_an_attempt_with_no_timeout(self, clock):
        """``budget_s`` without ``timeout`` still has to bind."""
        granted: list[float | None] = []
        real_wait_for = asyncio.wait_for

        async def _spy(coro, timeout):
            granted.append(timeout)
            return await real_wait_for(coro, timeout=timeout)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(retry_mod.asyncio, "wait_for", _spy)

            with pytest.raises(openai.RateLimitError):
                await call_with_retry(
                    lambda: _raise(_rate_limited()),
                    label="t",
                    max_attempts=1,
                    budget_s=7.0,
                )

        assert granted == [7.0]


async def _raise(exc: BaseException):
    raise exc


@pytest.mark.unit
class TestTheRetryAfterCapIsDerived:
    @pytest.mark.asyncio
    async def test_a_hint_over_half_the_remaining_budget_hands_off(self, clock):
        """Half, so the attempt after the wait has at least as long again.

        6s asked with 10s left: waiting leaves 4s for an attempt that may
        need 10, so the request would likely be wasted. Hand off instead.
        """
        calls = 0

        async def _count():
            nonlocal calls
            calls += 1
            raise _rate_limited(**{"retry-after": "6"})

        with pytest.raises(openai.RateLimitError):
            await call_with_retry(
                _count, label="t", max_attempts=3, base_delay=1.0, budget_s=10.0
            )

        assert calls == 1
        assert clock.slept == []

    @pytest.mark.asyncio
    async def test_a_hint_inside_half_is_honoured(self, clock):
        """4s asked with 10s left: waited, leaving 6s for the retry."""

        async def _asks():
            raise _rate_limited(**{"retry-after": "4"})

        with pytest.raises(openai.RateLimitError):
            await call_with_retry(
                _asks, label="t", max_attempts=2, base_delay=1.0, budget_s=10.0
            )

        assert clock.slept == [4.0]

    @pytest.mark.asyncio
    async def test_a_generous_budget_beats_the_fixed_cap(self, clock):
        """The derived cap must be able to exceed ``LLM_MAX_RETRY_AFTER_S``.

        The 5s constant is the answer for callers that declared nothing.
        A caller declaring 60s has said it can afford to wait, and
        overriding that would make the budget decorative.
        """
        assert retry_mod.LLM_MAX_RETRY_AFTER_S == 5.0

        async def _asks():
            raise _rate_limited(**{"retry-after": "20"})

        with pytest.raises(openai.RateLimitError):
            await call_with_retry(
                _asks, label="t", max_attempts=2, base_delay=1.0, budget_s=60.0
            )

        assert clock.slept == [20.0]

    @pytest.mark.asyncio
    async def test_without_a_budget_the_fixed_cap_still_applies(self, clock):
        """The control: no budget, so 20s is over the 5s cap and hands off."""
        calls = 0

        async def _count():
            nonlocal calls
            calls += 1
            raise _rate_limited(**{"retry-after": "20"})

        with pytest.raises(openai.RateLimitError):
            await call_with_retry(_count, label="t", max_attempts=3, base_delay=1.0)

        assert calls == 1
        assert clock.slept == []


@pytest.mark.unit
class TestItIsPerProvider:
    @pytest.mark.asyncio
    async def test_the_fallback_provider_gets_its_own_budget(self, clock):
        """Same semantics ``timeout`` already has, and what ``recall`` means.

        "one primary attempt (15s), then one fallback attempt (15s) ...
        worst case ~30s" is two budgets, not one shared 15s — so a primary
        that spends its whole budget must not leave the fallback unable to
        run at all.
        """
        primary, fallback = object(), object()
        seen: list[str] = []

        class _Config:
            def resolve_fallback(self):
                return ("gemini", "some-model")

        async def _call(provider):
            if provider is primary:
                seen.append("primary")
                clock.advance(10.0)  # spends the primary's entire budget
                raise _rate_limited()
            seen.append("fallback")
            return "answered"

        result = await call_with_fallback(
            "openai",
            _call,
            lambda: "the fake stub",
            tenant_config=_Config(),
            provider_factory=lambda name, _c, **_k: (
                primary if name == "openai" else fallback
            ),
            max_attempts=1,
            budget_s=10.0,
        )

        assert result == "answered"
        assert seen == ["primary", "fallback"]
