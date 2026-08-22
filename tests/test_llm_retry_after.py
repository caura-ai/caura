"""When a provider says when to come back, listen — or hand off.

#859 pinned the OpenAI SDK's internal retries to stop an invisible 3x
multiplier. That also dropped the one thing the SDK did better than us: it
honoured ``Retry-After``. This is that capability rebuilt at the layer
that actually owns retrying, where it is logged and inside the budget
accounting rather than hidden underneath it.

The interesting half is the refusal. Every budget above ``call_with_retry``
is small and fixed (10 s in ``dedup_judge``, 15 s in ``recall_service``,
30 s bulk, 35 s inline), and providers routinely ask for 60 s. Sleeping
that out is strictly worse than not retrying — the outer timeout fires
having spent the whole window asleep. So an over-budget hint ends the
retry loop, which is what lets ``call_with_fallback`` reach the SECOND
provider. That is a decision the old SDK-level retries could not make:
they had no idea a fallback existed.

Jitter belongs to the same fix rather than being a drive-by.
``BULK_ENRICHMENT_CONCURRENCY`` is 10, so a bulk write fires ten
enrichment calls at once; with a purely linear delay a rate-limited batch
comes back in lockstep one second later, which is how a rate limit
sustains itself.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import openai
import pytest

from common.llm import retry as retry_mod
from common.llm.retry import call_with_fallback, call_with_retry, retry_after_seconds
from tests._scoped_module import scoped


def _rate_limited(**headers: str) -> openai.RateLimitError:
    """A real 429 carrying *headers*, so the duck-typing meets a true shape."""
    response = httpx.Response(
        429,
        headers=headers,
        request=httpx.Request("POST", "http://provider/v1/chat/completions"),
    )
    return openai.RateLimitError("slow down", response=response, body=None)


@pytest.fixture
def slept(monkeypatch):
    """Record every sleep ``call_with_retry`` takes, instead of taking it.

    Scoped to ``retry_mod``'s own view of ``asyncio``. Patching
    ``retry_mod.asyncio.sleep`` would patch the shared module and record
    every task in the process — see ``tests/_scoped_module`` for the CI
    failure that cost.
    """
    recorded: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(retry_mod, "asyncio", scoped(asyncio, sleep=_fake_sleep))
    # Jitter off by default so a delay assertion is exact; the two tests
    # that care about jitter turn it back on themselves.
    monkeypatch.setattr(retry_mod, "LLM_RETRY_JITTER_FRACTION", 0.0)
    return recorded


async def _always(exc: BaseException):
    raise exc


@pytest.mark.unit
class TestTheRecorderRecordsOnlyUs:
    """Every delay assertion below is only worth what this class pins.

    caura#863 failed here with ``[0.7359102269208245, 1.0, 2.0]``: a
    leaked background task retrying ``/memories/similar-candidates``
    reached its third backoff inside this fixture's window, and the
    recorder took the credit. Nothing in the suite said the recorder was
    supposed to be ours alone, so nothing caught it.
    """

    @pytest.mark.asyncio
    async def test_a_sleep_taken_elsewhere_is_not_recorded(self, slept):
        """This module is not ``common.llm.retry``, so its sleeps are its own."""
        await asyncio.sleep(0)

        assert slept == []

    @pytest.mark.asyncio
    async def test_a_sleep_taken_elsewhere_still_really_happens(self, slept):
        """Scoping must not silence the rest of the process either.

        A globally faked ``sleep`` does not merely mis-attribute: it turns
        every other task's backoff into a no-op, so a leaked retry loop
        spins through its remaining attempts at once. Whatever else is
        running must be left alone, not sped up.
        """
        started = asyncio.get_running_loop().time()
        await asyncio.sleep(0.05)

        assert asyncio.get_running_loop().time() - started >= 0.05

    @pytest.mark.asyncio
    async def test_the_retry_loop_is_still_recorded(self, slept):
        """The inverse, and the one that keeps the rest of the file honest.

        A fixture scoped to the WRONG module records nothing at all, and
        every ``assert slept == [...]`` here would then be asserting on an
        empty list that no code path can fill.
        """
        with pytest.raises(openai.RateLimitError):
            await call_with_retry(
                lambda: _always(_rate_limited()),
                label="t",
                max_attempts=2,
                base_delay=1.0,
            )

        assert slept == [1.0]


@pytest.mark.unit
class TestTheHintIsHonoured:
    @pytest.mark.asyncio
    async def test_a_waitable_hint_replaces_our_guess(self, slept):
        """3s asked, 3s waited — not the 1s we would have chosen."""
        exc = _rate_limited(**{"retry-after": "3"})

        with pytest.raises(openai.RateLimitError):
            await call_with_retry(
                lambda: _always(exc), label="t", max_attempts=2, base_delay=1.0
            )

        assert slept == [3.0]

    @pytest.mark.asyncio
    async def test_a_hint_shorter_than_our_backoff_does_not_shorten_it(self, slept):
        """``max``, not replacement.

        A hint sooner than our own backoff would have us hammer a provider
        that is already refusing. Both floors hold: never sooner than we
        planned, never sooner than we were told.
        """
        exc = _rate_limited(**{"retry-after": "0.2"})

        with pytest.raises(openai.RateLimitError):
            await call_with_retry(
                lambda: _always(exc), label="t", max_attempts=2, base_delay=1.0
            )

        assert slept == [1.0]

    @pytest.mark.asyncio
    async def test_no_hint_keeps_the_linear_backoff(self, slept):
        """The control: absent the header, nothing about today changes."""
        exc = _rate_limited()

        with pytest.raises(openai.RateLimitError):
            await call_with_retry(
                lambda: _always(exc), label="t", max_attempts=3, base_delay=1.0
            )

        assert slept == [1.0, 2.0]


@pytest.mark.unit
class TestAnUnwaitableHintHandsOff:
    @pytest.mark.asyncio
    async def test_over_the_cap_stops_retrying_immediately(self, slept):
        """60s asked against a 5s cap: don't sleep, don't retry, raise now."""
        exc = _rate_limited(**{"retry-after": "60"})
        calls = 0

        async def _count():
            nonlocal calls
            calls += 1
            raise exc

        with pytest.raises(openai.RateLimitError):
            await call_with_retry(_count, label="t", max_attempts=3, base_delay=1.0)

        assert calls == 1
        assert slept == []

    @pytest.mark.asyncio
    async def test_and_that_is_what_reaches_the_fallback_provider(self, slept):
        """The payoff, and the thing SDK-level retries structurally could not do.

        A rate-limited primary should cost one request and then a hop, not
        a budget spent asleep. The SDK had no concept of a second provider,
        so its ``Retry-After`` compliance could only ever wait.
        """
        primary, fallback = object(), object()
        seen: list[str] = []

        class _Config:
            def resolve_fallback(self):
                return ("gemini", "some-model")

        def _factory(name, _config, **_kwargs):
            return primary if name == "openai" else fallback

        async def _call(provider):
            if provider is primary:
                seen.append("primary")
                raise _rate_limited(**{"retry-after": "60"})
            seen.append("fallback")
            return "answered by the fallback"

        result = await call_with_fallback(
            "openai",
            _call,
            lambda: "the fake stub",
            tenant_config=_Config(),
            provider_factory=_factory,
            max_attempts=3,
        )

        assert result == "answered by the fallback"
        # One primary attempt, not three, and no sleep before the hop.
        assert seen == ["primary", "fallback"]
        assert slept == []


@pytest.mark.unit
class TestHeaderParsing:
    def test_milliseconds_win_over_seconds(self):
        """Both are sent; the precise one is used."""
        assert retry_after_seconds(
            _rate_limited(**{"retry-after-ms": "5200", "retry-after": "5"})
        ) == pytest.approx(5.2)

    def test_an_http_date_is_understood(self):
        """RFC 9110 allows a date, and providers send them."""
        when = datetime.now(UTC) + timedelta(seconds=4)
        parsed = retry_after_seconds(
            _rate_limited(**{"retry-after": format_datetime(when)})
        )
        assert parsed == pytest.approx(4.0, abs=1.5)

    def test_a_date_in_the_past_clamps_to_zero(self):
        """Clock skew must not become a negative sleep."""
        when = datetime.now(UTC) - timedelta(minutes=5)
        assert retry_after_seconds(
            _rate_limited(**{"retry-after": format_datetime(when)})
        ) == pytest.approx(0.0)

    @pytest.mark.parametrize("garbage", ["soon", "", "-", "5 seconds", "NaNs"])
    def test_an_unparseable_hint_is_no_hint(self, garbage):
        """A malformed hint must not break the retry it was hinting about."""
        assert retry_after_seconds(_rate_limited(**{"retry-after": garbage})) is None

    @pytest.mark.parametrize("garbage", ["soon", "", "later"])
    def test_a_broken_ms_header_falls_through_to_the_good_one(self, garbage):
        """Preferring the precise header must cost nothing when it is junk.

        Both values arrive on the same response, so parsing the preferred
        one must not be able to discard the other — otherwise "prefer
        ``retry-after-ms``" quietly means "ignore ``Retry-After`` whenever
        the provider's ms header is malformed".
        """
        exc = _rate_limited(**{"retry-after-ms": garbage, "retry-after": "4"})
        assert retry_after_seconds(exc) == pytest.approx(4.0)

    def test_an_exception_with_no_response_is_no_hint(self):
        """Most failures here are timeouts and connection errors."""
        assert retry_after_seconds(TimeoutError("no response at all")) is None
        assert retry_after_seconds(_rate_limited()) is None


@pytest.mark.unit
class TestJitter:
    @pytest.mark.asyncio
    async def test_jitter_only_ever_lengthens_the_wait(self, slept, monkeypatch):
        """Bounded above and below: decorrelation must not become impatience."""
        monkeypatch.setattr(retry_mod, "LLM_RETRY_JITTER_FRACTION", 0.25)
        exc = _rate_limited()

        for _ in range(20):
            with pytest.raises(openai.RateLimitError):
                await call_with_retry(
                    lambda: _always(exc), label="t", max_attempts=2, base_delay=1.0
                )

        assert all(1.0 <= s <= 1.25 for s in slept), slept
        # Decorrelation is the whole point — a constant would defeat it.
        assert len(set(slept)) > 1

    @pytest.mark.asyncio
    async def test_jitter_never_undercuts_a_retry_after(self, slept, monkeypatch):
        """The header is a floor, so jitter is applied after the ``max``."""
        monkeypatch.setattr(retry_mod, "LLM_RETRY_JITTER_FRACTION", 0.25)
        exc = _rate_limited(**{"retry-after": "4"})

        for _ in range(20):
            with pytest.raises(openai.RateLimitError):
                await call_with_retry(
                    lambda: _always(exc), label="t", max_attempts=2, base_delay=1.0
                )

        assert all(4.0 <= s <= 5.0 for s in slept), slept

    @pytest.mark.asyncio
    async def test_a_negative_fraction_cannot_shorten_the_wait(
        self, slept, monkeypatch
    ):
        """An env var must not be able to invert the additive-only guarantee.

        ``read_float_env`` permits negatives, and a negative fraction makes
        ``random.uniform`` return an offset that SUBTRACTS — sleeping less
        than the provider asked for, which is exactly what the ordering
        here exists to prevent.

        Asserted at the point of use rather than on the constant: that way
        the guarantee holds however the value arrived, and the test does
        not depend on module-reload ordering.
        """
        monkeypatch.setattr(retry_mod, "LLM_RETRY_JITTER_FRACTION", -0.5)
        exc = _rate_limited(**{"retry-after": "4"})

        for _ in range(20):
            with pytest.raises(openai.RateLimitError):
                await call_with_retry(
                    lambda: _always(exc), label="t", max_attempts=2, base_delay=1.0
                )

        assert all(s == pytest.approx(4.0) for s in slept), slept
