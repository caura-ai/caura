"""Tests for ``core_api.config.bridge_credentials_to_environ`` (CAURA-595).

The bridge copies pydantic-settings ``.env``-loaded values into
``os.environ`` so the shared ``common.llm`` modules — which read env
vars directly — see them too. Without it, local-dev with API keys in
``.env`` (the documented shape) silently falls through to
``FakeLLMProvider``.
"""

from __future__ import annotations

import os

import pytest

from core_api.config import bridge_credentials_to_environ


# Bridge mutates ``os.environ`` directly (not via monkeypatch.setenv),
# so its writes leak across tests. Snapshot the credential-shaped keys
# before each test and restore on teardown so other test files (e.g.
# ``test_platform_providers``) don't inherit a polluted environment.
_BRIDGE_KEYS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY",
    "GEMINI_API_KEY",
    "ENTITY_EXTRACTION_PROVIDER",
    "ENTITY_EXTRACTION_MODEL",
    "OPENAI_REQUEST_TIMEOUT_SECONDS",
    "PLATFORM_LLM_PROVIDER",
    "PLATFORM_LLM_MODEL",
    "PLATFORM_LLM_API_KEY",
    "PLATFORM_LLM_GCP_PROJECT_ID",
    "PLATFORM_LLM_GCP_LOCATION",
)


@pytest.fixture(autouse=True)
def _restore_bridge_environ():
    """Snapshot and restore the credential-shaped env vars per-test."""
    snapshot = {k: os.environ.get(k) for k in _BRIDGE_KEYS}
    yield
    for k, v in snapshot.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.mark.unit
class TestBridgeCredentialsToEnviron:
    """Verify the settings → os.environ bridge."""

    def test_bridges_openai_key(self, monkeypatch):
        """``settings.openai_api_key`` lands in ``OPENAI_API_KEY``."""
        import core_api.config as cfg

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setattr(cfg.settings, "openai_api_key", "sk-from-dotenv")

        bridge_credentials_to_environ()

        assert os.environ["OPENAI_API_KEY"] == "sk-from-dotenv"

    def test_does_not_clobber_existing_env_value(self, monkeypatch):
        """Existing ``os.environ`` values win over ``settings.X``.

        Matches pydantic-settings' own precedence: shell env > ``.env``.
        """
        import core_api.config as cfg

        monkeypatch.setenv("OPENAI_API_KEY", "sk-from-shell")
        monkeypatch.setattr(cfg.settings, "openai_api_key", "sk-from-dotenv")

        bridge_credentials_to_environ()

        assert os.environ["OPENAI_API_KEY"] == "sk-from-shell"

    def test_skips_empty_settings_values(self, monkeypatch):
        """Empty / None settings don't pollute ``os.environ``."""
        import core_api.config as cfg

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(cfg.settings, "openai_api_key", None)
        monkeypatch.setattr(cfg.settings, "anthropic_api_key", "")

        bridge_credentials_to_environ()

        # Bridge skipped both — the shell env stays absent rather than
        # gaining a junk empty entry.
        assert os.environ.get("OPENAI_API_KEY", None) is None
        assert os.environ.get("ANTHROPIC_API_KEY", None) is None

    def test_bridges_platform_llm_key_via_secretstr(self, monkeypatch):
        """``settings.platform_llm_api_key`` is a ``SecretStr`` —
        bridge calls ``get_secret_value()``."""
        import core_api.config as cfg
        from pydantic import SecretStr

        monkeypatch.delenv("PLATFORM_LLM_API_KEY", raising=False)
        monkeypatch.setattr(
            cfg.settings, "platform_llm_api_key", SecretStr("sk-platform-from-dotenv")
        )

        bridge_credentials_to_environ()

        assert os.environ["PLATFORM_LLM_API_KEY"] == "sk-platform-from-dotenv"

    def test_real_provider_resolution_after_bridge(self, monkeypatch):
        """End-to-end: ``.env``-only key → real ``OpenAILLMProvider``,
        not ``FakeLLMProvider``. Matches the local-dev workflow the
        bridge restores."""
        import core_api.config as cfg

        # Simulate "key is in .env (settings) but NOT in os.environ".
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setattr(cfg.settings, "openai_api_key", "sk-from-dotenv")

        bridge_credentials_to_environ()

        from common.llm.providers.fake import FakeLLMProvider
        from common.llm.providers.openai import OpenAILLMProvider
        from common.llm.registry import get_llm_provider

        provider = get_llm_provider("openai", tenant_config=None)
        assert not isinstance(provider, FakeLLMProvider)
        assert isinstance(provider, OpenAILLMProvider)


@pytest.mark.unit
class TestEnrichmentPromptFormatStringEscape:
    """User content with ``{...}`` tokens MUST NOT crash the LLM call.

    Pre-fix: ``ENRICHMENT_PROMPT.format(content=content, today=...)``
    raised ``KeyError`` for any content containing ``{...}`` — caught
    by ``call_with_retry``, exhausted attempts, silent fall to
    heuristic.
    """

    @pytest.mark.asyncio
    async def test_curly_brace_content_does_not_crash_format(self, monkeypatch):
        """Content like ``"I used {memory_type} in my code"`` reaches
        the LLM stub instead of falling to ``fake_enrich``."""
        from common.enrichment.service import enrich_memory

        captured_prompts = []

        async def fake_complete_json(prompt: str):
            captured_prompts.append(prompt)
            return {"memory_type": "fact", "weight": 0.7, "title": "x"}

        from unittest.mock import AsyncMock, MagicMock

        mock_llm = AsyncMock()
        mock_llm.complete_json = fake_complete_json

        tc = MagicMock()
        tc.enrichment_provider = "openai"
        tc.enrichment_model = None

        async def mock_fallback(*args, **kwargs):
            call_fn = args[1] if len(args) > 1 else kwargs.get("call_fn")
            return await call_fn(mock_llm)

        monkeypatch.setattr(
            "common.enrichment.service.call_with_fallback",
            mock_fallback,
        )

        # The hostile input — pre-fix this would have raised KeyError
        # inside ``ENRICHMENT_PROMPT.format()``.
        result = await enrich_memory(
            "I used {memory_type} in my code and {0} broke",
            tenant_config=tc,
        )

        # LLM was actually invoked (not the heuristic fallback).
        assert len(captured_prompts) == 1
        # The user content survived intact in the rendered prompt
        # (escaped to ``{{...}}`` becomes ``{...}`` in the final string
        # via ``.format()``'s own un-escaping).
        assert "{memory_type}" in captured_prompts[0]
        assert "{0}" in captured_prompts[0]
        assert result.memory_type == "fact"
