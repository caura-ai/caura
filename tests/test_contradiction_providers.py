"""Multi-provider contradiction detection — OpenAI, Anthropic, OpenRouter.

Unit tests validate:
- Provider constants and base URLs
- API key resolution helpers
- Fallback chain logic
- OpenAI-compatible function dispatching
- Existing fake/none providers unchanged
"""

from unittest.mock import MagicMock

import pytest

from core_api.constants import (
    ANTHROPIC_CHAT_BASE_URL,
    ANTHROPIC_DEFAULT_MODEL,
    LLM_FALLBACK_MODEL_OPENAI,
    OPENAI_CHAT_BASE_URL,
    OPENROUTER_CHAT_BASE_URL,
    OPENROUTER_DEFAULT_MODEL,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProviderConstants:
    """Verify provider constants exist and are valid URLs/model names."""

    def test_openai_base_url(self):
        assert OPENAI_CHAT_BASE_URL == "https://api.openai.com/v1"

    def test_anthropic_base_url(self):
        assert ANTHROPIC_CHAT_BASE_URL == "https://api.anthropic.com/v1"

    def test_openrouter_base_url(self):
        assert OPENROUTER_CHAT_BASE_URL == "https://openrouter.ai/api/v1"

    def test_anthropic_default_model(self):
        assert (
            "claude" in ANTHROPIC_DEFAULT_MODEL.lower()
            or "haiku" in ANTHROPIC_DEFAULT_MODEL.lower()
        )

    def test_openrouter_default_model(self):
        assert OPENROUTER_DEFAULT_MODEL  # non-empty

    def test_openai_fallback_model(self):
        assert LLM_FALLBACK_MODEL_OPENAI == "gpt-5.4-nano"


# ---------------------------------------------------------------------------
# API key resolution
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHasApiKey:
    """Verify _has_api_key checks credentials correctly."""

    def test_vertex_rejected_as_tenant_provider(self):
        """Vertex is platform-tier only — tenant-facing has_credentials returns False."""
        from core_api.services.contradiction_detector import _has_api_key

        config = MagicMock()
        # Any attr pattern — vertex branch is removed, result is always False.
        assert _has_api_key("vertex", config) is False

    def test_openai_with_key(self):
        from core_api.services.contradiction_detector import _has_api_key

        config = MagicMock()
        config.openai_api_key = "sk-test"
        assert _has_api_key("openai", config) is True

    def test_openai_without_key(self, monkeypatch):
        from core_api.services.contradiction_detector import _has_api_key

        config = MagicMock()
        config.openai_api_key = None
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert _has_api_key("openai", config) is False

    def test_anthropic_with_key(self):
        from core_api.services.contradiction_detector import _has_api_key

        config = MagicMock()
        config.anthropic_api_key = "sk-ant-test"
        assert _has_api_key("anthropic", config) is True

    def test_openrouter_with_key(self):
        from core_api.services.contradiction_detector import _has_api_key

        config = MagicMock()
        config.openrouter_api_key = "sk-or-test"
        assert _has_api_key("openrouter", config) is True

    def test_unknown_provider(self):
        from core_api.services.contradiction_detector import _has_api_key

        assert _has_api_key("unknown", None) is False


# ---------------------------------------------------------------------------
# Provider dispatch
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestResolveOpenAICompatible:
    """Verify _resolve_openai_compatible returns correct (key, url, model) tuples."""

    def test_openai_resolution(self):
        from core_api.services.contradiction_detector import _resolve_openai_compatible

        config = MagicMock()
        config.openai_api_key = "sk-test"
        key, url, model = _resolve_openai_compatible("openai", config)
        assert key == "sk-test"
        assert url == OPENAI_CHAT_BASE_URL
        assert model == LLM_FALLBACK_MODEL_OPENAI

    def test_anthropic_resolution(self):
        from core_api.services.contradiction_detector import _resolve_openai_compatible

        config = MagicMock()
        config.anthropic_api_key = "sk-ant-test"
        key, url, model = _resolve_openai_compatible("anthropic", config)
        assert key == "sk-ant-test"
        assert url == ANTHROPIC_CHAT_BASE_URL
        assert model == ANTHROPIC_DEFAULT_MODEL

    def test_openrouter_resolution(self):
        from core_api.services.contradiction_detector import _resolve_openai_compatible

        config = MagicMock()
        config.openrouter_api_key = "sk-or-test"
        key, url, model = _resolve_openai_compatible("openrouter", config)
        assert key == "sk-or-test"
        assert url == OPENROUTER_CHAT_BASE_URL
        assert model == OPENROUTER_DEFAULT_MODEL

    def test_unknown_provider_returns_none(self):
        from core_api.services.contradiction_detector import _resolve_openai_compatible

        key, url, model = _resolve_openai_compatible("unknown", None)
        assert not key  # returns empty string for unknown providers


# ---------------------------------------------------------------------------
# Fallback chain
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFallbackChain:
    """Verify the fallback chain in _llm_contradiction_check.

    A4 #12 — judge now returns ``(verdict, confidence)``. These tests
    pin only the verdict bool; the heuristic fallback always emits
    confidence=0.50 (see ``_CONF_FALLBACK``), exercised in the A4 #12
    test module's malformed-response case.
    """

    @pytest.mark.asyncio
    async def test_none_provider_returns_false(self):
        from core_api.services.contradiction_detector import _llm_contradiction_check

        config = MagicMock()
        config.entity_extraction_provider = "none"
        verdict, _conf = await _llm_contradiction_check("a", "b", config)
        assert verdict is False

    @pytest.mark.asyncio
    async def test_fake_provider_uses_heuristic(self):
        from core_api.services.contradiction_detector import _llm_contradiction_check

        config = MagicMock()
        config.entity_extraction_provider = "fake"
        # No negation difference → False
        verdict, _conf = await _llm_contradiction_check(
            "the system is running", "the system is fast", config
        )
        assert verdict is False

    @pytest.mark.asyncio
    async def test_fake_provider_triggers_heuristic_via_fallback(self):
        """When provider resolves to FakeLLMProvider, call_with_fallback skips to fake_fn."""

        from core_api.services.contradiction_detector import _llm_contradiction_check

        config = MagicMock()
        config.entity_extraction_provider = "openai"
        config.openai_api_key = None  # no credentials -> FakeLLMProvider

        # Negation difference → heuristic returns True
        verdict, _conf = await _llm_contradiction_check(
            "the system is not running and it crashed",
            "the system is running and it works fine",
            config,
        )
        assert verdict is True

    @pytest.mark.asyncio
    async def test_no_credentials_heuristic_returns_false(self):
        """When no credentials and no negation difference, heuristic returns False."""

        from core_api.services.contradiction_detector import _llm_contradiction_check

        config = MagicMock()
        config.entity_extraction_provider = "openai"
        config.openai_api_key = None
        config.anthropic_api_key = None
        config.openrouter_api_key = None

        # No negation → heuristic returns False
        verdict, _conf = await _llm_contradiction_check(
            "the system works", "the system is fine", config
        )
        assert verdict is False


# ---------------------------------------------------------------------------
# Tenant settings integration
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTenantSettingsKeys:
    """Verify ResolvedConfig has anthropic/openrouter key properties."""

    def test_anthropic_key_from_settings(self):
        from core_api.services.organization_settings import ResolvedConfig

        config = ResolvedConfig({"api_keys": {}})
        # Falls back to global (None or empty string)
        assert not config.anthropic_api_key

    def test_openrouter_key_from_settings(self):
        from core_api.services.organization_settings import ResolvedConfig

        config = ResolvedConfig({"api_keys": {}})
        assert config.openrouter_api_key is None

    def test_has_anthropic_property(self):
        from core_api.services.organization_settings import ResolvedConfig

        assert hasattr(ResolvedConfig, "anthropic_api_key")

    def test_has_openrouter_property(self):
        from core_api.services.organization_settings import ResolvedConfig

        assert hasattr(ResolvedConfig, "openrouter_api_key")
