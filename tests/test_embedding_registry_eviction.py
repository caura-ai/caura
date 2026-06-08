"""Test that LRU eviction in common.embedding._registry closes the evicted
provider's httpx pool (CAURA-627 round-3).

The eviction path used to drop the reference and rely on GC; with the
explicit ``http_client=httpx.AsyncClient(...)`` we now pass to the OpenAI
SDK, the SDK no longer owns the client teardown, so the registry must
schedule ``aclose()`` itself on eviction.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import common.embedding._registry as registry_mod


@pytest.mark.asyncio
async def test_eviction_schedules_aclose_on_evicted_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the LRU cache evicts an entry, the evicted provider's
    ``aclose()`` must be awaited so the underlying httpx pool is
    closed cleanly. Without this, asyncio debug mode emits
    ``ResourceWarning: Unclosed <httpx.AsyncClient>`` on every
    eviction and the connections leak in long-lived processes."""
    # Tighten the cache to make eviction trivially observable.
    monkeypatch.setattr(registry_mod, "_OPENAI_CACHE_MAX", 1)
    monkeypatch.setattr(
        registry_mod, "_openai_provider_cache", registry_mod.OrderedDict()
    )

    # Track every provider instance we make and whether aclose was
    # awaited. Replace the provider class with a stand-in that
    # records the call without hitting the OpenAI SDK / network.
    aclose_calls: list[tuple[str, str]] = []

    class _FakeProvider:
        def __init__(
            self,
            *,
            api_key: str,
            model: str,
            base_url: str | None = None,
            send_dimensions: bool = True,
            query_instruction: str | None = None,
            truncate_to_dim: int | None = None,
        ) -> None:
            self.api_key = api_key
            self.model = model
            self.aclose = AsyncMock(
                side_effect=lambda: aclose_calls.append((api_key, model))
            )  # type: ignore[arg-type]

    monkeypatch.setattr(registry_mod, "OpenAIEmbeddingProvider", _FakeProvider)

    # ``_get_or_create_openai_provider`` grew four args after the
    # local-embedder branch landed (base_url / send_dimensions /
    # query_instruction / truncate_to_dim). Pass the no-op defaults
    # here — eviction is keyed on the full tuple so passing identical
    # extras across both calls keeps the test's intent (different
    # api_keys → distinct cache slots → first one evicted).
    _DEFAULTS = dict(
        base_url=None,
        send_dimensions=True,
        query_instruction=None,
        truncate_to_dim=None,
    )
    # First insert — no eviction yet.
    registry_mod._get_or_create_openai_provider("key-A", "model-1", **_DEFAULTS)
    assert aclose_calls == []

    # Second insert exceeds cap=1 → evicts (key-A, model-1).
    registry_mod._get_or_create_openai_provider("key-B", "model-1", **_DEFAULTS)

    # Yield to the event loop so the scheduled aclose() task runs.
    import asyncio

    await asyncio.sleep(0)
    await asyncio.sleep(0)  # belt-and-suspenders for any nested awaits

    assert aclose_calls == [("key-A", "model-1")], (
        f"expected aclose() called for evicted provider only; got {aclose_calls!r}"
    )


def test_eviction_outside_running_loop_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bare ``except RuntimeError`` covers callers that exercise
    the registry outside an asyncio event loop (e.g. early-startup
    config validation). The eviction must not crash; cleanup falls
    through to GC in that case."""
    monkeypatch.setattr(registry_mod, "_OPENAI_CACHE_MAX", 1)
    monkeypatch.setattr(
        registry_mod, "_openai_provider_cache", registry_mod.OrderedDict()
    )

    class _FakeProvider:
        def __init__(
            self,
            *,
            api_key: str,
            model: str,
            base_url: str | None = None,
            send_dimensions: bool = True,
            query_instruction: str | None = None,
            truncate_to_dim: int | None = None,
        ) -> None:
            self.api_key = api_key
            self.model = model

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(registry_mod, "OpenAIEmbeddingProvider", _FakeProvider)

    _DEFAULTS = dict(
        base_url=None,
        send_dimensions=True,
        query_instruction=None,
        truncate_to_dim=None,
    )
    registry_mod._get_or_create_openai_provider("key-A", "model-1", **_DEFAULTS)
    # No event loop running here — the second call triggers eviction
    # and must swallow the RuntimeError from get_running_loop().
    registry_mod._get_or_create_openai_provider("key-B", "model-1", **_DEFAULTS)
