"""Tests for the provider layer: protocols, registry, fakes, retry/fallback."""

from __future__ import annotations

import logging

import pytest

from common.embedding import (
    FakeEmbeddingProvider,
    fake_embedding,  # noqa: F401
    get_embedding_provider,
)
from common.embedding.providers.openai import OpenAIEmbeddingProvider
from core_api.constants import VECTOR_DIM
from core_api.protocols import (
    EmbeddingProvider,
    LLMProvider,
    STMBackend,
)
from core_api.providers import (
    get_llm_provider,
    get_stm_backend,
)
from core_api.providers._retry import call_with_fallback, call_with_retry
from core_api.providers.fake_provider import FakeLLMProvider
from core_api.providers.openai_provider import OpenAILLMProvider
from core_api.providers.vertex_provider import VertexLLMProvider

# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    """Verify that concrete providers satisfy the runtime-checkable protocols."""

    def test_fake_llm_satisfies_protocol(self):
        assert isinstance(FakeLLMProvider(), LLMProvider)

    def test_fake_embedding_satisfies_protocol(self):
        assert isinstance(FakeEmbeddingProvider(), EmbeddingProvider)

    def test_openai_llm_satisfies_protocol(self):
        p = OpenAILLMProvider(api_key="sk-test", model="gpt-4o-mini")
        assert isinstance(p, LLMProvider)

    def test_openai_embedding_satisfies_protocol(self):
        p = OpenAIEmbeddingProvider(api_key="sk-test")
        assert isinstance(p, EmbeddingProvider)

    def test_vertex_llm_satisfies_protocol(self):
        p = VertexLLMProvider(
            project_id="proj", location="us-central1", model="gemini-2.0-flash"
        )
        assert isinstance(p, LLMProvider)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    """Verify get_llm_provider / get_embedding_provider dispatch."""

    def test_get_llm_provider_fake(self):
        p = get_llm_provider("fake")
        assert isinstance(p, FakeLLMProvider)

    def test_get_embedding_provider_fake(self):
        p = get_embedding_provider("fake")
        assert isinstance(p, FakeEmbeddingProvider)

    def test_get_llm_provider_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown LLM provider"):
            get_llm_provider("unknown_name")

    def test_get_embedding_provider_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown embedding provider"):
            get_embedding_provider("unknown_name")


# ---------------------------------------------------------------------------
# Fake provider behaviour
# ---------------------------------------------------------------------------


class TestFakeProviders:
    """Verify fake provider return values."""

    @pytest.mark.asyncio
    async def test_fake_llm_complete_json(self):
        result = await FakeLLMProvider().complete_json("test")
        assert result == {}

    @pytest.mark.asyncio
    async def test_fake_llm_complete_text(self):
        result = await FakeLLMProvider().complete_text("test")
        assert result == ""

    @pytest.mark.asyncio
    async def test_fake_embedding_embed(self):
        vec = await FakeEmbeddingProvider().embed("test")
        assert isinstance(vec, list)
        assert len(vec) == VECTOR_DIM
        assert all(isinstance(v, float) for v in vec)

    @pytest.mark.asyncio
    async def test_fake_embedding_embed_batch(self):
        vecs = await FakeEmbeddingProvider().embed_batch(["a", "b"])
        assert len(vecs) == 2
        assert all(len(v) == VECTOR_DIM for v in vecs)


# ---------------------------------------------------------------------------
# call_with_retry
# ---------------------------------------------------------------------------


class TestCallWithRetry:
    """Verify retry semantics."""

    @pytest.mark.asyncio
    async def test_succeeds_on_first_try(self):
        result = await call_with_retry(
            lambda: _async_return("ok"),
            label="test",
            max_attempts=3,
            base_delay=0,
        )
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_retries_then_raises(self):
        call_count = 0

        async def _fail():
            nonlocal call_count
            call_count += 1
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            await call_with_retry(
                lambda: _fail(),
                label="test",
                max_attempts=3,
                base_delay=0,
            )
        assert call_count == 3


# ---------------------------------------------------------------------------
# call_with_fallback
# ---------------------------------------------------------------------------


class _MockRealProvider:
    """Non-fake provider stub for testing call_with_fallback with real providers."""

    @property
    def provider_name(self) -> str:
        return "mock-real"


class _MockTenantConfig:
    """Minimal tenant config that drives fallback resolution."""

    def __init__(self, fb_provider: str | None):
        self._fb = fb_provider

    def resolve_fallback(self) -> tuple[str | None, str | None]:
        return (self._fb, None)


class TestCallWithFallback:
    """Verify 3-tier fallback chain."""

    @pytest.mark.asyncio
    async def test_primary_succeeds(self):
        """Primary provider succeeds -- no fallback called."""
        calls: list[str] = []

        async def call_fn(provider):
            calls.append(provider.provider_name)
            return "primary-ok"

        result = await call_with_fallback(
            "mock",
            call_fn,
            fake_fn=lambda: "fake-val",
            provider_factory=lambda name, _tc, **kw: _MockRealProvider(),
        )
        assert result == "primary-ok"
        assert calls == ["mock-real"]

    @pytest.mark.asyncio
    async def test_explicit_fake_provider_skips_fallback(self):
        """primary_provider_name='fake' goes straight to fake_fn, never tries fallback."""
        call_fn_called = False

        async def call_fn(provider):
            nonlocal call_fn_called
            call_fn_called = True
            return "should-not-reach"

        tc = _MockTenantConfig(fb_provider="anthropic")

        result = await call_with_fallback(
            "fake",
            call_fn,
            fake_fn=lambda: "fake-val",
            tenant_config=tc,
            provider_factory=lambda name, _tc, **kw: _MockRealProvider(),
        )
        assert result == "fake-val"
        assert not call_fn_called

    @pytest.mark.asyncio
    async def test_fake_provider_skips_to_fake_fn(self):
        """FakeLLMProvider detected — call_fn is never called, fake_fn fires."""
        call_fn_called = False

        async def call_fn(provider):
            nonlocal call_fn_called
            call_fn_called = True
            return "should-not-reach"

        result = await call_with_fallback(
            "openai",
            call_fn,
            fake_fn=lambda: "fake-val",
            provider_factory=lambda name, _tc, **kw: FakeLLMProvider(),
        )
        assert result == "fake-val"
        assert not call_fn_called

    @pytest.mark.asyncio
    async def test_fake_primary_tries_real_fallback(self):
        """Primary is fake (no key), but fallback has credentials — fallback is used."""
        calls: list[str] = []

        async def call_fn(provider):
            calls.append(provider.provider_name)
            return "fallback-ok"

        tc = _MockTenantConfig(fb_provider="anthropic")

        def factory(name, _tc, **kw):
            if name == "openai":
                return FakeLLMProvider()  # no key
            return _MockRealProvider()  # anthropic has key

        result = await call_with_fallback(
            "openai",
            call_fn,
            fake_fn=lambda: "fake-val",
            tenant_config=tc,
            provider_factory=factory,
        )
        assert result == "fallback-ok"
        assert calls == ["mock-real"]

    @pytest.mark.asyncio
    async def test_fake_primary_fake_fallback_uses_fake_fn(self):
        """Both primary and fallback have no credentials — fake_fn is called."""
        call_fn_called = False

        async def call_fn(provider):
            nonlocal call_fn_called
            call_fn_called = True
            return "should-not-reach"

        tc = _MockTenantConfig(fb_provider="anthropic")

        result = await call_with_fallback(
            "openai",
            call_fn,
            fake_fn=lambda: "fake-val",
            tenant_config=tc,
            provider_factory=lambda name, _tc, **kw: FakeLLMProvider(),
        )
        assert result == "fake-val"
        assert not call_fn_called

    @pytest.mark.asyncio
    async def test_primary_fails_fallback_succeeds(self, caplog):
        """Primary fails, fallback provider succeeds."""
        attempt = 0

        async def call_fn(provider):
            nonlocal attempt
            attempt += 1
            if attempt <= 2:  # first 2 calls = primary retries
                raise RuntimeError("primary down")
            return "fallback-ok"

        tc = _MockTenantConfig(fb_provider="fallback")

        result = await call_with_fallback(
            "primary",
            call_fn,
            fake_fn=lambda: "fake-val",
            tenant_config=tc,
            provider_factory=lambda name, _tc, **kw: _MockRealProvider(),
        )
        assert result == "fallback-ok"
        # Control for the skip-reason logging below: the tier DID run, so nothing
        # may claim it was skipped.
        assert not [r for r in caplog.records if "SKIPPED" in r.getMessage()]

    @pytest.mark.asyncio
    async def test_all_fail_returns_fake(self):
        """Primary and fallback both fail -- fake_fn is called."""

        async def call_fn(provider):
            raise RuntimeError("down")

        tc = _MockTenantConfig(fb_provider="fallback-provider")

        result = await call_with_fallback(
            "primary",
            call_fn,
            fake_fn=lambda: {"empty": True},
            tenant_config=tc,
            provider_factory=lambda name, _tc, **kw: _MockRealProvider(),
        )
        assert result == {"empty": True}

    @pytest.mark.parametrize(
        "tenant_config,expected_reason",
        [
            # The prod shape: only one provider key set, so resolve_fallback()
            # returns (None, None) and the tier never runs.
            (_MockTenantConfig(fb_provider=None), "no fallback provider configured"),
            # Resolves, but to the provider that just failed.
            (
                _MockTenantConfig(fb_provider="primary"),
                "resolved fallback is the primary",
            ),
            # No config at all — e.g. a caller that never plumbed one through.
            (None, "no tenant config exposing resolve_fallback()"),
        ],
    )
    @pytest.mark.asyncio
    async def test_skipped_fallback_tier_says_so(
        self, caplog, tenant_config, expected_reason
    ):
        """A silently-skipped fallback tier must announce itself.

        Every skip path logged nothing, so "All LLM providers failed" was the only
        trace — and it reads as though a second provider had been tried. Prod ran
        3 days with 82 of those lines and zero fallback attempts.
        """

        async def call_fn(provider):
            raise RuntimeError("down")

        with caplog.at_level(logging.WARNING, logger="common.llm.retry"):
            result = await call_with_fallback(
                "primary",
                call_fn,
                fake_fn=lambda: {"empty": True},
                tenant_config=tenant_config,
                provider_factory=lambda name, _tc, **kw: _MockRealProvider(),
            )

        assert result == {"empty": True}
        skip_lines = [
            r.getMessage() for r in caplog.records if "SKIPPED" in r.getMessage()
        ]
        assert len(skip_lines) == 1, f"expected exactly one skip line, got {skip_lines}"
        assert expected_reason in skip_lines[0]


# ---------------------------------------------------------------------------
# Backward-compat re-exports
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    """Verify that the canonical import paths still work."""

    def test_fake_embedding_from_provider(self):
        from common.embedding import fake_embedding as fe1

        assert callable(fe1)

    def test_fake_embedding_from_core_embedding(self):
        from common.embedding import fake_embedding as fe2

        assert callable(fe2)

    def test_both_return_same_result(self):
        from common.embedding import fake_embedding as fe_core
        from common.embedding import fake_embedding as fe_prov

        assert fe_core("hello world") == fe_prov("hello world")


# ---------------------------------------------------------------------------
# Infrastructure protocol conformance
# ---------------------------------------------------------------------------


class _FakeSTM:
    async def get_notes(self, tenant_id, agent_id, limit=50):
        return []

    async def post_note(self, tenant_id, agent_id, entry):
        pass

    async def clear_notes(self, tenant_id, agent_id):
        pass

    async def get_bulletin(self, tenant_id, fleet_id, limit=100):
        return []

    async def post_bulletin(self, tenant_id, fleet_id, entry):
        pass

    async def clear_bulletin(self, tenant_id, fleet_id):
        pass


class TestInfraProtocolConformance:
    """Verify that a minimal fake satisfies the runtime-checkable protocol."""

    def test_stm_backend(self):
        assert isinstance(_FakeSTM(), STMBackend)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _async_return(val):
    return val


# ---------------------------------------------------------------------------
# Concrete implementation conformance
# ---------------------------------------------------------------------------


class TestConcreteConformance:
    """Verify that the OSS implementation satisfies its protocol."""

    def test_inmemory_stm(self):
        from core_api.providers.inmemory_stm import InMemorySTM

        assert isinstance(InMemorySTM(), STMBackend)


# ---------------------------------------------------------------------------
# InMemorySTM behavioral tests
# ---------------------------------------------------------------------------


class TestInMemorySTM:
    """Behavioral tests for InMemorySTM."""

    @pytest.mark.asyncio
    async def test_notes_roundtrip(self):
        from core_api.providers.inmemory_stm import InMemorySTM

        stm = InMemorySTM()
        await stm.post_note("t1", "agent-1", {"content": "test note"})
        notes = await stm.get_notes("t1", "agent-1")
        assert len(notes) == 1
        assert notes[0]["content"] == "test note"

    @pytest.mark.asyncio
    async def test_notes_empty(self):
        from core_api.providers.inmemory_stm import InMemorySTM

        stm = InMemorySTM()
        notes = await stm.get_notes("t1", "unknown")
        assert notes == []

    @pytest.mark.asyncio
    async def test_notes_clear(self):
        from core_api.providers.inmemory_stm import InMemorySTM

        stm = InMemorySTM()
        await stm.post_note("t1", "agent-1", {"content": "temp"})
        await stm.clear_notes("t1", "agent-1")
        notes = await stm.get_notes("t1", "agent-1")
        assert notes == []

    @pytest.mark.asyncio
    async def test_bulletin_order(self):
        from core_api.providers.inmemory_stm import InMemorySTM

        stm = InMemorySTM()
        await stm.post_bulletin("t1", "fleet-1", {"msg": "first"})
        await stm.post_bulletin("t1", "fleet-1", {"msg": "second"})
        await stm.post_bulletin("t1", "fleet-1", {"msg": "third"})
        entries = await stm.get_bulletin("t1", "fleet-1")
        assert len(entries) == 3
        assert entries[0]["msg"] == "third"  # newest first
        assert entries[2]["msg"] == "first"

    @pytest.mark.asyncio
    async def test_bulletin_cap(self):
        from core_api.providers.inmemory_stm import InMemorySTM

        stm = InMemorySTM(bulletin_max_entries=3)
        for i in range(5):
            await stm.post_bulletin("t1", "fleet-1", {"n": i})
        entries = await stm.get_bulletin("t1", "fleet-1")
        assert len(entries) == 3
        assert entries[0]["n"] == 4  # newest

    @pytest.mark.asyncio
    async def test_bulletin_empty(self):
        from core_api.providers.inmemory_stm import InMemorySTM

        stm = InMemorySTM()
        entries = await stm.get_bulletin("t1", "unknown")
        assert entries == []

    @pytest.mark.asyncio
    async def test_bulletin_clear(self):
        from core_api.providers.inmemory_stm import InMemorySTM

        stm = InMemorySTM()
        await stm.post_bulletin("t1", "fleet-1", {"msg": "temp"})
        await stm.clear_bulletin("t1", "fleet-1")
        entries = await stm.get_bulletin("t1", "fleet-1")
        assert entries == []


# ---------------------------------------------------------------------------
# Infrastructure registry tests
# ---------------------------------------------------------------------------


class TestInfraRegistry:
    """Verify the one surviving infrastructure backend factory.

    Four siblings were removed with the backends they built; ``stm`` is the
    only one a setting (``settings.stm_backend``) ever named.
    """

    def test_get_stm_backend_memory(self):
        s = get_stm_backend("memory")
        assert isinstance(s, STMBackend)

    def test_get_stm_backend_unknown(self):
        with pytest.raises(ValueError, match="Unknown STM backend"):
            get_stm_backend("unknown")
