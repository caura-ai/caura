"""09/02 M-36 — one pooled client per configuration, not per call.

`OpenAILLMProvider.__init__` builds an `httpx.AsyncClient` with its own
connection pool. `get_llm_provider` is called on EVERY LLM call — `retry.py`
invokes `provider_factory` for the primary provider and again for the fallback
— so each call minted a fresh pool and nothing ever closed it.

`OpenAILLMProvider.aclose` already existed, and its own docstring named this
exact failure ("a leak in long-lived processes that rotate client instances").
It had no caller anywhere in the repo.
"""

import asyncio
import inspect

import pytest

from common.llm import registry
from common.llm.registry import get_llm_provider, reset_provider_cache

pytestmark = pytest.mark.unit

_KEY = "sk-test-m36-aaaaaaaaaaaaaaaaaaaaaaaa"


@pytest.fixture(autouse=True)
def _clean_cache():
    reset_provider_cache()
    yield
    reset_provider_cache()


def _openai(monkeypatch, *, key=_KEY, model="gpt-4o-mini", timeout="30"):
    monkeypatch.setenv("OPENAI_API_KEY", key)
    monkeypatch.setenv("OPENAI_REQUEST_TIMEOUT_SECONDS", timeout)
    from types import SimpleNamespace

    return get_llm_provider(
        "openai",
        SimpleNamespace(openai_api_key=key, enrichment_model=model),
        model_override=model,
    )


def test_the_same_configuration_reuses_one_provider(monkeypatch):
    """The fix. Two calls with identical config must not build two pools."""
    a = _openai(monkeypatch)
    b = _openai(monkeypatch)
    assert a is b


def test_a_different_model_gets_its_own_provider(monkeypatch):
    """The key is the whole configuration, not just the provider name."""
    a = _openai(monkeypatch, model="gpt-4o-mini")
    b = _openai(monkeypatch, model="gpt-4o")
    assert a is not b


def test_a_different_api_key_gets_its_own_provider(monkeypatch):
    """A key of just ``name`` would pin the first tenant's credentials for the
    life of the process — a cross-tenant credential leak, not a perf bug."""
    a = _openai(monkeypatch, key=_KEY)
    b = _openai(monkeypatch, key="sk-test-m36-bbbbbbbbbbbbbbbbbbbbbbbb")
    assert a is not b


def test_a_changed_timeout_env_still_takes_effect(monkeypatch):
    """``request_timeout`` is read from os.environ at construction, and the
    call site documents why it must not use the import-time constant. Caching
    must not freeze that — so the timeout is part of the key."""
    a = _openai(monkeypatch, timeout="30")
    b = _openai(monkeypatch, timeout="90")
    assert a is not b


def test_the_cache_key_holds_no_secret_the_provider_does_not_already_hold(
    monkeypatch,
):
    """An earlier revision hashed the api key so the cache would not "retain a
    secret". That was theatre: the cached VALUE is the provider, and it holds
    the same key in memory for exactly as long as the cache holds the tuple.
    CodeQL flagged the hash (``py/weak-sensitive-data-hashing``) and deleting
    it was the right response — hashing bought no reduction in exposure while
    adding a collision that would hand one tenant's provider to another.

    So this asserts the property that actually matters: the key never outlives
    the provider, and carries nothing extra.
    """
    p = _openai(monkeypatch)
    keys = list(registry._PROVIDER_CACHE)
    assert len(keys) == 1
    assert _KEY in keys[0], "the key is compared verbatim, not digested"
    assert getattr(p, "_api_key", _KEY) == _KEY

    # And it is dropped with the provider, not kept beyond it.
    reset_provider_cache()
    assert not list(registry._PROVIDER_CACHE)


def test_no_cryptographic_hashing_of_credentials_creeps_back(monkeypatch):
    """Guards the CodeQL finding at its source rather than by suppression."""
    src = inspect.getsource(registry)
    assert "hashlib" not in src


def test_the_cache_is_bounded(monkeypatch):
    """A many-tenant process must not grow pools without limit."""
    for i in range(registry._PROVIDER_CACHE_MAX + 12):
        _openai(monkeypatch, model=f"model-{i}")
    assert len(registry._PROVIDER_CACHE) <= registry._PROVIDER_CACHE_MAX


def test_eviction_is_lru_not_fifo(monkeypatch):
    """Hot configurations should keep their pools; a rare one pays the
    reconnect. FIFO would evict the busiest tenant first."""
    first = _openai(monkeypatch, model="model-hot")
    for i in range(registry._PROVIDER_CACHE_MAX - 1):
        _openai(monkeypatch, model=f"model-{i}")
        _openai(monkeypatch, model="model-hot")  # keep it warm
    _openai(monkeypatch, model="model-overflow")
    assert _openai(monkeypatch, model="model-hot") is first


def test_only_the_pool_owning_provider_is_cached():
    """Gemini and Fake hold no client, so caching them would buy nothing and
    add a lifetime to reason about."""
    src = inspect.getsource(registry.get_llm_provider)
    assert "_PROVIDER_CACHE" in src
    gemini_branch = src[src.index("ProviderName.GEMINI") :]
    assert "_PROVIDER_CACHE" not in gemini_branch


async def test_an_evicted_provider_gets_closed():
    """The leak is only fixed if eviction closes the pool it drops.

    Written as a real async test rather than ``asyncio.run`` inside a sync one:
    spinning up a second loop inside a pytest-asyncio session tears down the
    loop the rest of the suite is using, which shows up as unrelated async
    tests reporting "coroutine was never awaited" — 81 of them, when I first
    wrote this.
    """

    class _P:
        def __init__(self):
            self.closed = False

        async def aclose(self):
            self.closed = True

    p = _P()
    registry._close_evicted(p)
    # Two yields: one for create_task to schedule, one for aclose to run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert p.closed is True


def test_closing_without_a_running_loop_is_a_no_op():
    """``get_llm_provider`` is sync. With no loop there is nothing to await on,
    and dropping the provider is exactly today's behaviour — but it must not
    raise on the path that is trying to hand back a working provider."""

    class _P:
        async def aclose(self):  # pragma: no cover - must not be reached
            raise AssertionError("should not run without a loop")

    registry._close_evicted(_P())


def test_a_provider_without_aclose_is_tolerated():
    registry._close_evicted(object())


def test_aclose_still_exists_on_the_provider():
    """Pins the method this fix finally calls. If it is ever removed, eviction
    silently stops closing pools and the leak returns."""
    from common.llm.providers.openai import OpenAILLMProvider

    assert callable(OpenAILLMProvider.aclose)
