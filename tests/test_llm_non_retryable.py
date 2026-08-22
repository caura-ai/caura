"""``non_retryable`` bounds retries within a provider, not the fallback chain.

A retry can only help if the call might return something different. Entity
extraction pins a seed so retries reproduce byte-identical output, which makes
a shape failure guaranteed to recur — the second attempt is pure waste. Every
other caller here is unseeded, so its retries are worth having; hence opt-in
rather than a global classification. See ``call_with_retry``'s docstring.
"""

from __future__ import annotations

import pytest

from common.llm.retry import call_with_fallback, call_with_retry

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class Shape(ValueError):
    """Stands in for a deterministic shape failure."""


class Transport(RuntimeError):
    """Stands in for a retryable transport failure."""


def _counting(exc: BaseException):
    """An always-failing coro_fn that records how many times it ran."""
    calls: list[int] = []

    async def _fn():
        calls.append(1)
        raise exc

    return _fn, calls


async def test_declared_type_is_not_retried() -> None:
    fn, calls = _counting(Shape("bad shape"))

    with pytest.raises(Shape):
        await call_with_retry(fn, label="t", max_attempts=3, base_delay=0, non_retryable=(Shape,))

    assert len(calls) == 1, f"a declared-deterministic failure must run once; ran {len(calls)}x"


async def test_undeclared_type_still_retries() -> None:
    """Control: the classification must be narrow, not a blanket fail-fast."""
    fn, calls = _counting(Transport("timeout"))

    with pytest.raises(Transport):
        await call_with_retry(fn, label="t", max_attempts=3, base_delay=0, non_retryable=(Shape,))

    assert len(calls) == 3, f"a transport failure must still use its budget; ran {len(calls)}x"


async def test_default_is_a_no_op_for_existing_callers() -> None:
    """The parameter defaults to (), so ~10 untouched callers are unaffected.

    ``isinstance(exc, ())`` is False for every exception, which is what makes
    the default a true no-op rather than a behaviour change.
    """
    fn, calls = _counting(Shape("bad shape"))

    with pytest.raises(Shape):
        await call_with_retry(fn, label="t", max_attempts=3, base_delay=0)

    assert len(calls) == 3, "without opting in, nothing may change"


async def test_subclasses_are_covered() -> None:
    """``isinstance`` semantics: declaring a base covers its subclasses."""

    class Narrower(Shape):
        pass

    fn, calls = _counting(Narrower("bad shape"))

    with pytest.raises(Narrower):
        await call_with_retry(fn, label="t", max_attempts=3, base_delay=0, non_retryable=(Shape,))

    assert len(calls) == 1


async def test_a_success_after_a_retryable_failure_still_succeeds() -> None:
    """Control: the happy path through the retry loop is unchanged."""
    attempts: list[int] = []

    async def _fn():
        attempts.append(1)
        if len(attempts) == 1:
            raise Transport("first one fails")
        return "ok"

    got = await call_with_retry(
        _fn, label="t", max_attempts=3, base_delay=0, non_retryable=(Shape,)
    )

    assert got == "ok"
    assert len(attempts) == 2


# ---------------------------------------------------------------------------
# The load-bearing property: skipping retries must NOT skip the fallback
# ---------------------------------------------------------------------------


class _Provider:
    is_fake = False

    def __init__(self, name: str) -> None:
        self.name = name


class _Cfg:
    """Minimal tenant config exposing a fallback provider."""

    def resolve_fallback(self, *_a, **_k):
        # Real contract: ``(provider_name, model)`` — retry.py unpacks two.
        return ("gemini", None)


async def test_fallback_provider_still_runs_after_a_non_retryable_primary() -> None:
    """The point of raising instead of returning empty.

    A deterministic failure means "this model cannot parse it", not "no model
    can" — the alternative provider is a different model and may well succeed.
    Skipping the wasted re-ask must not cost the provider hop.
    """
    seen: list[str] = []

    async def _call_fn(provider):
        seen.append(provider.name)
        if provider.name == "openai":
            raise Shape("primary mangles it")
        return "fallback-result"

    got = await call_with_fallback(
        primary_provider_name="openai",
        call_fn=_call_fn,
        fake_fn=lambda: "fake-result",
        tenant_config=_Cfg(),
        service_label="t",
        max_attempts=3,
        provider_factory=lambda name, _cfg, **_k: _Provider(name),
        non_retryable=(Shape,),
    )

    assert got == "fallback-result", f"the fallback provider must get its turn; got {got!r}"
    # One primary attempt (not three), then the fallback.
    assert seen == ["openai", "gemini"], f"expected one try each; got {seen!r}"


async def test_fake_fn_is_the_last_resort_when_both_providers_are_deterministic() -> None:
    """If every provider fails the same way, the heuristic still answers.

    ``call_with_fallback``'s contract is that it always returns something.
    """
    seen: list[str] = []

    async def _call_fn(provider):
        seen.append(provider.name)
        raise Shape("nobody parses it")

    got = await call_with_fallback(
        primary_provider_name="openai",
        call_fn=_call_fn,
        fake_fn=lambda: "fake-result",
        tenant_config=_Cfg(),
        service_label="t",
        max_attempts=3,
        provider_factory=lambda name, _cfg, **_k: _Provider(name),
        non_retryable=(Shape,),
    )

    assert got == "fake-result"
    # Two providers, one attempt each — six attempts before the fix.
    assert seen == ["openai", "gemini"], f"expected one try per provider; got {seen!r}"
