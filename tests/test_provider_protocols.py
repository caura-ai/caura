"""Tests for the provider layer: protocols, registry, fakes, retry/fallback."""

from __future__ import annotations

import asyncio

import pytest

from uuid import uuid4

from core_api.constants import VECTOR_DIM
from core_api.protocols import (
    ConflictResolver,
    ConflictResult,
    EmbeddingProvider,
    Identity,
    IdentityResolver,
    JobQueue,
    LLMProvider,
    Resolution,
    STMBackend,
    SearchFilters,
    StorageBackend,
)
from common.embedding import (
    FakeEmbeddingProvider,
    fake_embedding,  # noqa: F401
    get_embedding_provider,
)
from core_api.providers import (
    get_conflict_resolver,
    get_identity_resolver,
    get_job_queue,
    get_llm_provider,
    get_stm_backend,
    get_storage_backend,
)
from common.embedding.providers.openai import OpenAIEmbeddingProvider
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
    async def test_primary_fails_fallback_succeeds(self):
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


class _FakeStorage:
    async def store(self, tenant_id, content, embedding=None, *, metadata=None):
        return "1"

    async def get(self, tenant_id, memory_id):
        return None

    async def update(self, tenant_id, memory_id, fields):
        pass

    async def delete(self, tenant_id, memory_id):
        return False

    async def search(self, query_embedding, query_text, filters, *, limit=10):
        return []

    async def graph_traverse(self, tenant_id, entity_id, *, hops=1):
        return []


class _FakeJobQueue:
    async def enqueue(self, func, *args, **kwargs):
        return "job-1"


class _FakeIdentityResolver:
    async def resolve(self, context):
        return Identity(tenant_id="t1")


class _FakeConflictResolver:
    async def resolve(self, conflict):
        return Resolution(action="keep_both")


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
    """Verify that minimal fakes satisfy the runtime-checkable protocols."""

    def test_storage_backend(self):
        assert isinstance(_FakeStorage(), StorageBackend)

    def test_job_queue(self):
        assert isinstance(_FakeJobQueue(), JobQueue)

    def test_identity_resolver(self):
        assert isinstance(_FakeIdentityResolver(), IdentityResolver)

    def test_conflict_resolver(self):
        assert isinstance(_FakeConflictResolver(), ConflictResolver)

    def test_stm_backend(self):
        assert isinstance(_FakeSTM(), STMBackend)


# ---------------------------------------------------------------------------
# Supporting type instantiation
# ---------------------------------------------------------------------------


class TestProtocolTypes:
    """Verify that supporting dataclasses can be created with defaults."""

    def test_search_filters_minimal(self):
        f = SearchFilters(tenant_id="t1")
        assert f.tenant_id == "t1"
        assert f.fleet_ids is None
        assert f.status is None

    def test_identity_minimal(self):
        i = Identity(tenant_id="t1")
        assert i.tenant_id == "t1"
        assert i.roles == []
        assert i.metadata == {}

    def test_identity_with_roles(self):
        i = Identity(tenant_id="t1", roles=["admin", "viewer"])
        assert len(i.roles) == 2

    def test_conflict_result_minimal(self):
        c = ConflictResult(existing_memory_id=uuid4())
        assert c.reason == ""
        assert c.confidence == 0.0

    def test_resolution_minimal(self):
        r = Resolution(action="keep_both")
        assert r.winner_id is None
        assert r.metadata == {}

    def test_resolution_with_winner(self):
        wid = uuid4()
        lid = uuid4()
        r = Resolution(action="supersede", winner_id=wid, loser_id=lid)
        assert r.winner_id == wid
        assert r.loser_id == lid


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _async_return(val):
    return val


# ---------------------------------------------------------------------------
# Concrete implementation conformance
# ---------------------------------------------------------------------------


class TestConcreteConformance:
    """Verify that the new OSS implementations satisfy their protocols."""

    def test_sqlite_backend(self):
        from core_api.providers.sqlite_backend import SqliteBackend

        assert isinstance(SqliteBackend(":memory:"), StorageBackend)

    def test_config_identity(self):
        from core_api.providers.config_identity import ConfigIdentity

        assert isinstance(ConfigIdentity(), IdentityResolver)

    def test_inprocess_queue(self):
        from core_api.providers.inprocess_queue import InProcessQueue

        assert isinstance(InProcessQueue(), JobQueue)

    def test_inmemory_stm(self):
        from core_api.providers.inmemory_stm import InMemorySTM

        assert isinstance(InMemorySTM(), STMBackend)

    def test_manual_resolver(self):
        from core_api.providers.manual_resolver import ManualResolver

        assert isinstance(ManualResolver(), ConflictResolver)


# ---------------------------------------------------------------------------
# SqliteBackend behavioral tests
# ---------------------------------------------------------------------------


class TestSqliteBackend:
    """Behavioral tests for SqliteBackend using in-memory database."""

    @pytest.fixture
    def backend(self):
        from core_api.providers.sqlite_backend import SqliteBackend

        return SqliteBackend(":memory:")

    @pytest.mark.asyncio
    async def test_store_and_get(self, backend):
        mid = await backend.store("t1", "hello world", metadata={"key": "val"})
        assert isinstance(mid, str)
        row = await backend.get("t1", mid)
        assert row is not None
        assert row["content"] == "hello world"
        assert row["tenant_id"] == "t1"

    @pytest.mark.asyncio
    async def test_store_with_embedding(self, backend):
        emb = [0.1] * VECTOR_DIM
        mid = await backend.store("t1", "with vector", embedding=emb)
        row = await backend.get("t1", mid)
        assert row is not None
        assert row["embedding"] is not None
        assert len(row["embedding"]) == VECTOR_DIM

    @pytest.mark.asyncio
    async def test_get_wrong_tenant(self, backend):
        mid = await backend.store("t1", "secret")
        assert await backend.get("t2", mid) is None

    @pytest.mark.asyncio
    async def test_update(self, backend):
        mid = await backend.store("t1", "original")
        await backend.update("t1", mid, {"title": "updated-title"})
        row = await backend.get("t1", mid)
        assert row["title"] == "updated-title"

    @pytest.mark.asyncio
    async def test_delete(self, backend):
        mid = await backend.store("t1", "to delete")
        assert await backend.delete("t1", mid) is True
        assert await backend.get("t1", mid) is None
        assert await backend.delete("t1", mid) is False

    @pytest.mark.asyncio
    async def test_search_vector(self, backend):
        # Store 3 memories with known embeddings
        e1 = [1.0] + [0.0] * (VECTOR_DIM - 1)
        e2 = [0.0, 1.0] + [0.0] * (VECTOR_DIM - 2)
        e3 = [0.9] + [0.1] * (VECTOR_DIM - 1)  # similar to e1
        await backend.store("t1", "memory one", embedding=e1)
        await backend.store("t1", "memory two", embedding=e2)
        await backend.store("t1", "memory three", embedding=e3)

        query_emb = [1.0] + [0.0] * (VECTOR_DIM - 1)
        results = await backend.search(
            query_emb, "", SearchFilters(tenant_id="t1"), limit=3
        )
        assert len(results) >= 2
        # First result should be the most similar to query
        assert results[0]["content"] in ("memory one", "memory three")

    @pytest.mark.asyncio
    async def test_search_text(self, backend):
        await backend.store("t1", "the quick brown fox")
        await backend.store("t1", "lazy dog sleeping")
        results = await backend.search(
            [], "fox", SearchFilters(tenant_id="t1"), limit=10
        )
        contents = [r["content"] for r in results]
        assert "the quick brown fox" in contents

    @pytest.mark.asyncio
    async def test_graph_traverse_empty(self, backend):
        result = await backend.graph_traverse("t1", "nonexistent-entity")
        assert result == []


# ---------------------------------------------------------------------------
# ConfigIdentity behavioral tests
# ---------------------------------------------------------------------------


class TestConfigIdentity:
    """Behavioral tests for ConfigIdentity."""

    @pytest.mark.asyncio
    async def test_defaults(self):
        from core_api.providers.config_identity import ConfigIdentity

        resolver = ConfigIdentity()
        identity = await resolver.resolve({})
        assert identity.tenant_id == "default"
        assert identity.roles == ["admin"]

    @pytest.mark.asyncio
    async def test_custom_tenant(self):
        from core_api.providers.config_identity import ConfigIdentity

        resolver = ConfigIdentity(tenant_id="my-tenant", roles=["viewer"])
        identity = await resolver.resolve({})
        assert identity.tenant_id == "my-tenant"
        assert identity.roles == ["viewer"]

    @pytest.mark.asyncio
    async def test_context_override(self):
        from core_api.providers.config_identity import ConfigIdentity

        resolver = ConfigIdentity()
        identity = await resolver.resolve({"tenant_id": "override"})
        assert identity.tenant_id == "override"


# ---------------------------------------------------------------------------
# InProcessQueue behavioral tests
# ---------------------------------------------------------------------------


class TestInProcessQueue:
    """Behavioral tests for InProcessQueue."""

    @pytest.mark.asyncio
    async def test_enqueue_returns_job_id(self):
        from core_api.providers.inprocess_queue import InProcessQueue

        q = InProcessQueue()
        called = asyncio.Event()

        async def work():
            called.set()

        job_id = await q.enqueue(work)
        assert isinstance(job_id, str)
        assert len(job_id) > 0
        await asyncio.sleep(0.05)
        assert called.is_set()

    @pytest.mark.asyncio
    async def test_enqueue_with_args(self):
        from core_api.providers.inprocess_queue import InProcessQueue

        q = InProcessQueue()
        results = []

        async def work(a, b, key="default"):
            results.append((a, b, key))

        await q.enqueue(work, 1, 2, key="custom")
        await asyncio.sleep(0.05)
        assert results == [(1, 2, "custom")]


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
# ManualResolver behavioral tests
# ---------------------------------------------------------------------------


class TestManualResolver:
    """Behavioral tests for ManualResolver."""

    @pytest.mark.asyncio
    async def test_keeps_both(self):
        from core_api.providers.manual_resolver import ManualResolver

        resolver = ManualResolver()
        conflict = ConflictResult(
            existing_memory_id=uuid4(),
            new_memory_id=uuid4(),
            reason="semantic_conflict",
        )
        result = await resolver.resolve(conflict)
        assert result.action == "keep_both"
        assert result.explanation != ""


# ---------------------------------------------------------------------------
# Infrastructure registry tests
# ---------------------------------------------------------------------------


class TestInfraRegistry:
    """Verify infrastructure backend factory functions."""

    def test_get_storage_backend_sqlite(self):
        backend = get_storage_backend("sqlite", db_path=":memory:")
        assert isinstance(backend, StorageBackend)

    def test_get_storage_backend_unknown(self):
        with pytest.raises(ValueError, match="Unknown storage backend"):
            get_storage_backend("unknown")

    def test_get_job_queue_inprocess(self):
        q = get_job_queue("inprocess")
        assert isinstance(q, JobQueue)

    def test_get_job_queue_unknown(self):
        with pytest.raises(ValueError, match="Unknown job queue"):
            get_job_queue("unknown")

    def test_get_identity_resolver_config(self):
        r = get_identity_resolver("config")
        assert isinstance(r, IdentityResolver)

    def test_get_identity_resolver_unknown(self):
        with pytest.raises(ValueError, match="Unknown identity resolver"):
            get_identity_resolver("unknown")

    def test_get_conflict_resolver_manual(self):
        r = get_conflict_resolver("manual")
        assert isinstance(r, ConflictResolver)

    def test_get_conflict_resolver_unknown(self):
        with pytest.raises(ValueError, match="Unknown conflict resolver"):
            get_conflict_resolver("unknown")

    def test_get_stm_backend_memory(self):
        s = get_stm_backend("memory")
        assert isinstance(s, STMBackend)

    def test_get_stm_backend_unknown(self):
        with pytest.raises(ValueError, match="Unknown STM backend"):
            get_stm_backend("unknown")
