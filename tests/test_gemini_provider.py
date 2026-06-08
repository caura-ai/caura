"""Tests for the Gemini LLM provider (Developer API, API-key auth)."""

from __future__ import annotations

import pytest

from core_api.constants import GEMINI_DEFAULT_MODEL
from core_api.protocols import LLMProvider
from core_api.providers._credentials import has_credentials, resolve_gemini_config
from core_api.providers._registry import get_llm_provider
from core_api.providers.fake_provider import FakeLLMProvider
from core_api.providers.gemini_provider import GeminiLLMProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeTenantConfig:
    """Minimal tenant config stub — only attributes the registry reads."""

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def _reset_platform_singletons():
    import common.embedding._platform as embedding_mod
    import common.llm._platform as llm_mod

    llm_mod._platform_llm = None
    embedding_mod._platform_embedding = None


@pytest.fixture(autouse=True)
def _clean_platform():
    _reset_platform_singletons()
    yield
    _reset_platform_singletons()


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestGeminiProtocol:
    def test_satisfies_llm_protocol(self):
        p = GeminiLLMProvider(api_key="AIza-test", model=GEMINI_DEFAULT_MODEL)
        assert isinstance(p, LLMProvider)

    def test_provider_name(self):
        p = GeminiLLMProvider(api_key="AIza-test", model="gemini-2.0-flash")
        assert p.provider_name == "gemini"

    def test_model_property(self):
        p = GeminiLLMProvider(api_key="AIza-test", model="gemini-2.0-flash")
        assert p.model == "gemini-2.0-flash"


# ---------------------------------------------------------------------------
# Credential resolution
# ---------------------------------------------------------------------------


class TestGeminiCredentials:
    def test_has_credentials_global_fallback(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "AIza-global")
        assert has_credentials("gemini") is True

    def test_has_credentials_false_when_empty(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        assert has_credentials("gemini") is False

    def test_has_credentials_tenant_overrides(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        tc = _FakeTenantConfig(gemini_api_key="AIza-tenant")
        assert has_credentials("gemini", tc) is True

    def test_resolve_gemini_config_tenant_wins(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "AIza-global")
        tc = _FakeTenantConfig(
            gemini_api_key="AIza-tenant", enrichment_model="gemini-2.0-flash"
        )
        key, model = resolve_gemini_config(tc)
        assert key == "AIza-tenant"
        assert model == "gemini-2.0-flash"

    def test_resolve_gemini_config_respects_global_entity_extraction_model(
        self, monkeypatch
    ):
        """No tenant config → falls back to ENTITY_EXTRACTION_MODEL env var."""
        monkeypatch.setenv("GEMINI_API_KEY", "AIza-global")
        monkeypatch.delenv("ENRICHMENT_MODEL", raising=False)
        monkeypatch.setenv("ENTITY_EXTRACTION_MODEL", "gemini-2.0-flash")
        key, model = resolve_gemini_config(None)
        assert key == "AIza-global"
        assert model == "gemini-2.0-flash"

    def test_resolve_gemini_config_falls_back_to_default_when_global_empty(
        self, monkeypatch
    ):
        """If all upstream sources are empty, GEMINI_DEFAULT_MODEL is used."""
        monkeypatch.setenv("GEMINI_API_KEY", "AIza-global")
        monkeypatch.delenv("ENRICHMENT_MODEL", raising=False)
        monkeypatch.delenv("ENTITY_EXTRACTION_MODEL", raising=False)
        key, model = resolve_gemini_config(None)
        assert key == "AIza-global"
        assert model == GEMINI_DEFAULT_MODEL

    def test_resolve_gemini_config_empty(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("ENRICHMENT_MODEL", raising=False)
        monkeypatch.delenv("ENTITY_EXTRACTION_MODEL", raising=False)
        key, model = resolve_gemini_config(None)
        assert key == ""
        assert model == GEMINI_DEFAULT_MODEL


# ---------------------------------------------------------------------------
# Registry dispatch
# ---------------------------------------------------------------------------


class TestGeminiRegistry:
    def test_tier1_tenant_key_returns_gemini_provider(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        provider = get_llm_provider(
            "gemini",
            _FakeTenantConfig(
                gemini_api_key="AIza-tenant", enrichment_model=GEMINI_DEFAULT_MODEL
            ),
        )
        assert isinstance(provider, GeminiLLMProvider)
        assert provider.model == GEMINI_DEFAULT_MODEL

    def test_model_override_respected(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "AIza-global")
        provider = get_llm_provider(
            "gemini", None, model_override="gemini-2.5-flash-lite"
        )
        assert isinstance(provider, GeminiLLMProvider)
        assert provider.model == "gemini-2.5-flash-lite"

    def test_tier3_fake_when_no_key_and_no_platform(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        provider = get_llm_provider("gemini", _FakeTenantConfig(gemini_api_key=""))
        assert isinstance(provider, FakeLLMProvider)

    def test_tier2_platform_used_when_no_tenant_key(self, monkeypatch):
        monkeypatch.setenv("PLATFORM_LLM_PROVIDER", "vertex")
        monkeypatch.setenv("PLATFORM_LLM_GCP_PROJECT_ID", "platform-proj")
        monkeypatch.setenv("PLATFORM_LLM_GCP_LOCATION", "us-central1")
        monkeypatch.setenv("PLATFORM_LLM_MODEL", "gemini-3.1-flash-lite-preview")
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)

        from core_api.providers._platform import (
            get_platform_llm,
            init_platform_providers,
        )

        init_platform_providers()
        provider = get_llm_provider("gemini", _FakeTenantConfig(gemini_api_key=""))
        assert provider is get_platform_llm()
