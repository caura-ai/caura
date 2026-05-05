"""Tests for platform default provider singletons and three-tier resolution.

Unit tests only — no database, no real API calls.
"""

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reset_platform_singletons():
    """Reset the module-level singletons so each test starts clean.

    CAURA-594 + CAURA-595 extraction: both the embedding and LLM
    singletons live in ``common/`` now (``common.embedding._platform``
    and ``common.llm._platform``). Reset both so a leftover
    platform-tier provider from a prior test can't satisfy a "no
    platform" assertion in the next.
    """
    import common.embedding._platform as embedding_mod
    import common.llm._platform as llm_mod

    llm_mod._platform_llm = None
    embedding_mod._platform_embedding = None


@pytest.fixture(autouse=True)
def _clean_platform(monkeypatch):
    """Ensure platform singletons are reset before and after each test."""
    _reset_platform_singletons()
    yield
    _reset_platform_singletons()


# ---------------------------------------------------------------------------
# Group 1: Initialization
# ---------------------------------------------------------------------------


class TestInitPlatformProviders:
    """init_platform_providers() builds correct singletons from env vars."""

    def test_init_vertex_llm(self, monkeypatch):
        monkeypatch.setenv("PLATFORM_LLM_PROVIDER", "vertex")
        monkeypatch.setenv("PLATFORM_LLM_GCP_PROJECT_ID", "test-proj")
        monkeypatch.setenv("PLATFORM_LLM_GCP_LOCATION", "us-central1")
        monkeypatch.setenv("PLATFORM_LLM_MODEL", "gemini-3.1-flash-lite-preview")
        # Re-instantiate settings to pick up env changes
        self._reinit_settings(monkeypatch)

        from core_api.providers._platform import (
            get_platform_llm,
            init_platform_providers,
        )
        from core_api.providers.vertex_provider import VertexLLMProvider

        init_platform_providers()
        llm = get_platform_llm()
        assert isinstance(llm, VertexLLMProvider)
        assert llm._project_id == "test-proj"
        assert llm._model == "gemini-3.1-flash-lite-preview"

    def test_init_openai_embedding(self, monkeypatch):
        monkeypatch.setenv("PLATFORM_EMBEDDING_PROVIDER", "openai")
        monkeypatch.setenv("PLATFORM_EMBEDDING_API_KEY", "sk-test-key-123")
        monkeypatch.setenv("PLATFORM_EMBEDDING_MODEL", "text-embedding-3-small")
        self._reinit_settings(monkeypatch)

        from core_api.providers._platform import (
            get_platform_embedding,
            init_platform_providers,
        )
        from common.embedding.providers.openai import OpenAIEmbeddingProvider

        init_platform_providers()
        emb = get_platform_embedding()
        assert isinstance(emb, OpenAIEmbeddingProvider)

    def test_init_empty_returns_none(self, monkeypatch):
        monkeypatch.setenv("PLATFORM_LLM_PROVIDER", "")
        monkeypatch.setenv("PLATFORM_EMBEDDING_PROVIDER", "")
        self._reinit_settings(monkeypatch)

        from core_api.providers._platform import (
            get_platform_embedding,
            get_platform_llm,
            init_platform_providers,
        )

        init_platform_providers()
        assert get_platform_llm() is None
        assert get_platform_embedding() is None

    def test_init_vertex_no_project_warns(self, monkeypatch, caplog):
        monkeypatch.setenv("PLATFORM_LLM_PROVIDER", "vertex")
        monkeypatch.setenv("PLATFORM_LLM_GCP_PROJECT_ID", "")
        self._reinit_settings(monkeypatch)

        from core_api.providers._platform import (
            get_platform_llm,
            init_platform_providers,
        )

        init_platform_providers()
        assert get_platform_llm() is None
        assert "no PLATFORM_LLM_GCP_PROJECT_ID" in caplog.text

    def test_init_openai_embedding_no_key_warns(self, monkeypatch, caplog):
        monkeypatch.setenv("PLATFORM_EMBEDDING_PROVIDER", "openai")
        monkeypatch.setenv("PLATFORM_EMBEDDING_API_KEY", "")
        self._reinit_settings(monkeypatch)

        from core_api.providers._platform import (
            get_platform_embedding,
            init_platform_providers,
        )

        init_platform_providers()
        assert get_platform_embedding() is None
        assert "no PLATFORM_EMBEDDING_API_KEY" in caplog.text

    def test_unknown_llm_provider_warns(self, monkeypatch, caplog):
        monkeypatch.setenv("PLATFORM_LLM_PROVIDER", "typo-vertex")
        self._reinit_settings(monkeypatch)

        from core_api.providers._platform import (
            get_platform_llm,
            init_platform_providers,
        )

        init_platform_providers()
        assert get_platform_llm() is None
        assert "Unknown PLATFORM_LLM_PROVIDER" in caplog.text

    def test_unknown_embedding_provider_warns(self, monkeypatch, caplog):
        monkeypatch.setenv("PLATFORM_EMBEDDING_PROVIDER", "typo-openai")
        self._reinit_settings(monkeypatch)

        from core_api.providers._platform import (
            get_platform_embedding,
            init_platform_providers,
        )

        init_platform_providers()
        assert get_platform_embedding() is None
        assert "Unknown PLATFORM_EMBEDDING_PROVIDER" in caplog.text

    def test_vertex_embedding_uses_own_gcp_config(self, monkeypatch):
        monkeypatch.setenv("PLATFORM_EMBEDDING_PROVIDER", "vertex")
        monkeypatch.setenv("PLATFORM_EMBEDDING_GCP_PROJECT_ID", "emb-proj")
        monkeypatch.setenv("PLATFORM_EMBEDDING_GCP_LOCATION", "europe-west1")
        monkeypatch.setenv("PLATFORM_LLM_GCP_PROJECT_ID", "llm-proj")
        self._reinit_settings(monkeypatch)

        from core_api.providers._platform import (
            get_platform_embedding,
            init_platform_providers,
        )
        from common.embedding.providers.vertex import VertexEmbeddingProvider

        init_platform_providers()
        emb = get_platform_embedding()
        assert isinstance(emb, VertexEmbeddingProvider)
        assert emb._project_id == "emb-proj"

    def test_vertex_embedding_falls_back_to_llm_gcp_config(self, monkeypatch):
        monkeypatch.setenv("PLATFORM_EMBEDDING_PROVIDER", "vertex")
        monkeypatch.setenv("PLATFORM_EMBEDDING_GCP_PROJECT_ID", "")
        monkeypatch.setenv("PLATFORM_LLM_GCP_PROJECT_ID", "shared-proj")
        monkeypatch.setenv("PLATFORM_LLM_GCP_LOCATION", "us-east1")
        self._reinit_settings(monkeypatch)

        from core_api.providers._platform import (
            get_platform_embedding,
            init_platform_providers,
        )
        from common.embedding.providers.vertex import VertexEmbeddingProvider

        init_platform_providers()
        emb = get_platform_embedding()
        assert isinstance(emb, VertexEmbeddingProvider)
        assert emb._project_id == "shared-proj"

    def test_init_openai_llm(self, monkeypatch):
        monkeypatch.setenv("PLATFORM_LLM_PROVIDER", "openai")
        monkeypatch.setenv("PLATFORM_LLM_API_KEY", "sk-platform-llm")
        monkeypatch.setenv("PLATFORM_LLM_MODEL", "gpt-4o-mini")
        self._reinit_settings(monkeypatch)

        from core_api.providers._platform import (
            get_platform_llm,
            init_platform_providers,
        )
        from core_api.providers.openai_provider import OpenAILLMProvider

        init_platform_providers()
        llm = get_platform_llm()
        assert isinstance(llm, OpenAILLMProvider)

    def test_init_openai_llm_no_key_warns(self, monkeypatch, caplog):
        monkeypatch.setenv("PLATFORM_LLM_PROVIDER", "openai")
        monkeypatch.setenv("PLATFORM_LLM_API_KEY", "")
        self._reinit_settings(monkeypatch)

        from core_api.providers._platform import (
            get_platform_llm,
            init_platform_providers,
        )

        init_platform_providers()
        assert get_platform_llm() is None
        assert "no PLATFORM_LLM_API_KEY" in caplog.text

    def test_constructor_exception_leaves_singleton_none(self, monkeypatch):
        """If provider constructor raises, singleton stays None."""
        monkeypatch.setenv("PLATFORM_LLM_PROVIDER", "vertex")
        monkeypatch.setenv("PLATFORM_LLM_GCP_PROJECT_ID", "proj")
        monkeypatch.setenv("PLATFORM_LLM_MODEL", "gemini")
        self._reinit_settings(monkeypatch)

        from unittest.mock import patch

        from core_api.providers._platform import (
            get_platform_init_errors,
            get_platform_llm,
            init_platform_providers,
        )

        with patch(
            "common.llm.providers.vertex.VertexLLMProvider",
            side_effect=RuntimeError("SDK missing"),
        ):
            init_platform_providers()

        assert get_platform_llm() is None
        assert "vertex-llm" in get_platform_init_errors()

    def test_init_errors_cleared_on_reinit(self, monkeypatch):
        """Errors list is reset on each init call."""
        monkeypatch.setenv("PLATFORM_LLM_PROVIDER", "vertex")
        monkeypatch.setenv("PLATFORM_LLM_GCP_PROJECT_ID", "proj")
        monkeypatch.setenv("PLATFORM_LLM_MODEL", "gemini")
        self._reinit_settings(monkeypatch)

        from unittest.mock import patch

        from core_api.providers._platform import (
            get_platform_init_errors,
            init_platform_providers,
        )

        with patch(
            "common.llm.providers.vertex.VertexLLMProvider",
            side_effect=RuntimeError("fail"),
        ):
            init_platform_providers()
        assert len(get_platform_init_errors()) == 1

        # Second init without the mock — errors should be cleared
        init_platform_providers()
        assert len(get_platform_init_errors()) == 0

    @staticmethod
    def _reinit_settings(monkeypatch):
        """Force Settings to re-read from env."""
        import core_api.config as cfg

        monkeypatch.setattr(cfg, "settings", cfg.Settings())


# ---------------------------------------------------------------------------
# Group 2: LLM tier resolution
# ---------------------------------------------------------------------------


class TestLLMTierResolution:
    """get_llm_provider three-tier: tenant key → platform → fake."""

    def test_tier1_tenant_key_used(self, monkeypatch):
        """Tenant with API key gets their own provider, not platform."""
        self._setup_platform_llm(monkeypatch)

        from core_api.providers._registry import get_llm_provider
        from core_api.providers.openai_provider import OpenAILLMProvider

        provider = get_llm_provider(
            "openai", _FakeTenantConfig(openai_api_key="sk-tenant-key")
        )
        assert isinstance(provider, OpenAILLMProvider)

    def test_tier2_platform_when_no_tenant_key(self, monkeypatch):
        """No tenant key → platform singleton returned."""
        self._setup_platform_llm(monkeypatch)

        from core_api.providers._platform import get_platform_llm
        from core_api.providers._registry import get_llm_provider

        provider = get_llm_provider("openai", _FakeTenantConfig(openai_api_key=""))
        assert provider is get_platform_llm()

    def test_tier3_fake_when_no_platform(self, monkeypatch):
        """No tenant key, no platform → FakeLLMProvider."""
        self._ensure_no_platform(monkeypatch)

        from core_api.providers._registry import get_llm_provider
        from core_api.providers.fake_provider import FakeLLMProvider

        provider = get_llm_provider("openai", _FakeTenantConfig(openai_api_key=""))
        assert isinstance(provider, FakeLLMProvider)

    def test_none_provider_name_falls_back_to_platform(self, monkeypatch):
        """``provider_name=None`` (tenant has no override AND env default
        unset) MUST use the platform LLM if configured rather than
        raising ValueError ("Unknown LLM provider: None") and falling
        through ``call_with_fallback``'s misleading "primary failed
        after retries" path. This is the bug surfaced live on staging
        2026-04-26 with CAURA-595 contradiction-detection."""
        self._setup_platform_llm(monkeypatch)

        from core_api.providers._platform import get_platform_llm
        from core_api.providers._registry import get_llm_provider

        provider = get_llm_provider(None, _FakeTenantConfig())
        assert provider is get_platform_llm()

    def test_empty_string_provider_name_falls_back_to_platform(self, monkeypatch):
        """Empty-string variant of the None case (some callers pass ``""``
        instead of None when chaining ``.get(... , "")`` defaults)."""
        self._setup_platform_llm(monkeypatch)

        from core_api.providers._platform import get_platform_llm
        from core_api.providers._registry import get_llm_provider

        provider = get_llm_provider("", _FakeTenantConfig())
        assert provider is get_platform_llm()

    def test_none_provider_name_no_platform_returns_fake(self, monkeypatch):
        """Without platform LLM configured, ``None`` provider name still
        returns FakeLLMProvider rather than raising — keeps
        ``call_with_fallback``'s short-circuit at line 148 effective."""
        self._ensure_no_platform(monkeypatch)

        from core_api.providers._registry import get_llm_provider
        from core_api.providers.fake_provider import FakeLLMProvider

        provider = get_llm_provider(None, _FakeTenantConfig())
        assert isinstance(provider, FakeLLMProvider)

    def test_vertex_rejected_as_tenant_provider(self, monkeypatch):
        """Vertex is platform-tier only — rejected as tenant-facing name."""
        self._setup_platform_llm(monkeypatch)

        from core_api.providers._registry import get_llm_provider

        with pytest.raises(ValueError, match="Unknown LLM provider"):
            get_llm_provider("vertex", _FakeTenantConfig())

    @staticmethod
    def _setup_platform_llm(monkeypatch):
        monkeypatch.setenv("PLATFORM_LLM_PROVIDER", "vertex")
        monkeypatch.setenv("PLATFORM_LLM_GCP_PROJECT_ID", "platform-proj")
        monkeypatch.setenv("PLATFORM_LLM_GCP_LOCATION", "us-central1")
        monkeypatch.setenv("PLATFORM_LLM_MODEL", "gemini-3.1-flash-lite-preview")
        import core_api.config as cfg

        monkeypatch.setattr(cfg, "settings", cfg.Settings())
        # Clear any global openai key so tier-1 requires tenant key
        monkeypatch.setattr(cfg.settings, "openai_api_key", None)

        from core_api.providers._platform import init_platform_providers

        init_platform_providers()

    @staticmethod
    def _ensure_no_platform(monkeypatch):
        monkeypatch.setenv("PLATFORM_LLM_PROVIDER", "")
        monkeypatch.setenv("PLATFORM_EMBEDDING_PROVIDER", "")
        import core_api.config as cfg

        monkeypatch.setattr(cfg, "settings", cfg.Settings())
        monkeypatch.setattr(cfg.settings, "openai_api_key", None)


# ---------------------------------------------------------------------------
# Group 3: Embedding tier resolution
# ---------------------------------------------------------------------------


class TestEmbeddingTierResolution:
    """get_embedding_provider three-tier: tenant key → platform → fake."""

    def test_tier1_tenant_key_used(self, monkeypatch):
        """Tenant with API key gets their own provider, not platform."""
        self._setup_platform_embedding(monkeypatch)

        from common.embedding import get_embedding_provider
        from common.embedding.providers.openai import OpenAIEmbeddingProvider

        provider = get_embedding_provider(
            "openai", _FakeTenantConfig(openai_api_key="sk-tenant")
        )
        assert isinstance(provider, OpenAIEmbeddingProvider)

    def test_tier2_platform_when_no_key(self, monkeypatch):
        """No tenant key → platform singleton."""
        self._setup_platform_embedding(monkeypatch)

        from common.embedding import get_platform_embedding
        from common.embedding import get_embedding_provider

        provider = get_embedding_provider(
            "openai", _FakeTenantConfig(openai_api_key="")
        )
        assert provider is get_platform_embedding()

    def test_tier3_fake_when_no_platform(self, monkeypatch):
        """No tenant key, no platform → FakeEmbeddingProvider."""
        self._ensure_no_platform(monkeypatch)

        from common.embedding import get_embedding_provider
        from common.embedding import FakeEmbeddingProvider

        provider = get_embedding_provider(
            "openai", _FakeTenantConfig(openai_api_key="")
        )
        assert isinstance(provider, FakeEmbeddingProvider)

    @staticmethod
    def _setup_platform_embedding(monkeypatch):
        monkeypatch.setenv("PLATFORM_EMBEDDING_PROVIDER", "openai")
        monkeypatch.setenv("PLATFORM_EMBEDDING_API_KEY", "sk-platform-emb")
        monkeypatch.setenv("PLATFORM_EMBEDDING_MODEL", "text-embedding-3-small")
        import core_api.config as cfg

        monkeypatch.setattr(cfg, "settings", cfg.Settings())
        monkeypatch.setattr(cfg.settings, "openai_api_key", None)

        from core_api.providers._platform import init_platform_providers

        init_platform_providers()

    @staticmethod
    def _ensure_no_platform(monkeypatch):
        monkeypatch.setenv("PLATFORM_LLM_PROVIDER", "")
        monkeypatch.setenv("PLATFORM_EMBEDDING_PROVIDER", "")
        import core_api.config as cfg

        monkeypatch.setattr(cfg, "settings", cfg.Settings())
        monkeypatch.setattr(cfg.settings, "openai_api_key", None)


# ---------------------------------------------------------------------------
# Group 4: Security — platform keys don't leak
# ---------------------------------------------------------------------------


class TestPlatformSecurity:
    """Platform keys must not leak into tenant-configurable code paths."""

    def test_platform_key_not_in_resolve_openai_compatible(self, monkeypatch):
        """resolve_openai_compatible must not see platform keys."""
        monkeypatch.setenv("PLATFORM_EMBEDDING_PROVIDER", "openai")
        monkeypatch.setenv("PLATFORM_EMBEDDING_API_KEY", "sk-platform-secret")
        import core_api.config as cfg

        monkeypatch.setattr(cfg, "settings", cfg.Settings())
        monkeypatch.setattr(cfg.settings, "openai_api_key", None)

        from core_api.providers._credentials import resolve_openai_compatible

        api_key, _base_url, _model = resolve_openai_compatible("openai", None)
        assert api_key == ""
        assert "sk-platform-secret" not in api_key

    def test_platform_singleton_identity(self, monkeypatch):
        """get_platform_llm() returns the same object on repeated calls."""
        monkeypatch.setenv("PLATFORM_LLM_PROVIDER", "vertex")
        monkeypatch.setenv("PLATFORM_LLM_GCP_PROJECT_ID", "proj")
        monkeypatch.setenv("PLATFORM_LLM_MODEL", "gemini")
        import core_api.config as cfg

        monkeypatch.setattr(cfg, "settings", cfg.Settings())

        from core_api.providers._platform import (
            get_platform_llm,
            init_platform_providers,
        )

        init_platform_providers()
        a = get_platform_llm()
        b = get_platform_llm()
        assert a is b

    def test_platform_key_not_in_tenant_config(self, monkeypatch):
        """ResolvedConfig should not expose platform keys."""
        monkeypatch.setenv("PLATFORM_EMBEDDING_PROVIDER", "openai")
        monkeypatch.setenv("PLATFORM_EMBEDDING_API_KEY", "sk-platform-secret")
        import core_api.config as cfg

        monkeypatch.setattr(cfg, "settings", cfg.Settings())
        monkeypatch.setattr(cfg.settings, "openai_api_key", None)

        from core_api.services.organization_settings import ResolvedConfig

        config = ResolvedConfig({})
        # ResolvedConfig.openai_api_key reads tenant settings then global settings
        # Platform keys must not appear in either path
        assert config.openai_api_key != "sk-platform-secret"


# ---------------------------------------------------------------------------
# Group 5: has_credentials unchanged
# ---------------------------------------------------------------------------


class TestHasCredentialsUnchanged:
    """has_credentials must not report platform keys as tenant credentials."""

    def test_has_credentials_false_with_platform(self, monkeypatch):
        monkeypatch.setenv("PLATFORM_EMBEDDING_PROVIDER", "openai")
        monkeypatch.setenv("PLATFORM_EMBEDDING_API_KEY", "sk-platform")
        import core_api.config as cfg

        monkeypatch.setattr(cfg, "settings", cfg.Settings())
        monkeypatch.setattr(cfg.settings, "openai_api_key", None)

        from core_api.providers._credentials import has_credentials

        assert has_credentials("openai", None) is False


# ---------------------------------------------------------------------------
# Fake tenant config for tests
# ---------------------------------------------------------------------------


class _FakeTenantConfig:
    """Minimal tenant config stub with only the attributes the registry reads."""

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)
