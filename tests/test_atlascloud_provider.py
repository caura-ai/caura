"""Atlas Cloud provider wiring.

Atlas Cloud is an OpenAI-compatible gateway, so it rides the same
``OpenAILLMProvider`` path as OpenRouter — these tests pin the pieces that
make that work: the enum member, the constants, tenant/env credential
resolution, and membership in the OpenAI-compatible set.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from common.llm._credentials import (
    _env_key,
    has_credentials,
    resolve_openai_compatible,
)
from common.llm.constants import ATLASCLOUD_CHAT_BASE_URL, ATLASCLOUD_DEFAULT_MODEL
from common.llm.registry import _OPENAI_COMPATIBLE
from common.provider_names import ProviderName

pytestmark = pytest.mark.unit


class TestAtlasCloudConstants:
    def test_provider_name_member(self):
        assert ProviderName.ATLASCLOUD == "atlascloud"

    def test_base_url(self):
        assert ATLASCLOUD_CHAT_BASE_URL == "https://api.atlascloud.ai/v1"

    def test_default_model(self):
        assert ATLASCLOUD_DEFAULT_MODEL  # non-empty

    def test_is_openai_compatible(self):
        assert ProviderName.ATLASCLOUD in _OPENAI_COMPATIBLE


class TestAtlasCloudCredentials:
    @pytest.mark.parametrize(
        "model_attr", ["enrichment_model", "contradiction_model", "recall_model"]
    )
    @pytest.mark.parametrize(
        "model", ["openai/gpt-4.1-mini", "gpt-4.1-mini", "claude-sonnet-4-6"]
    )
    def test_resolve_preserves_tenant_model(self, monkeypatch, model_attr, model):
        monkeypatch.setenv(model_attr.upper(), "openai/env-fallback")
        config = SimpleNamespace(**{model_attr: model})
        assert (
            resolve_openai_compatible("atlascloud", config, model_attr=model_attr)[2]
            == model
        )

    @pytest.mark.parametrize("model", ["gpt-4.1-mini", "claude-sonnet-4-6"])
    def test_resolve_uses_per_service_env_model(self, monkeypatch, model):
        monkeypatch.setenv("RECALL_MODEL", model)
        config = SimpleNamespace(enrichment_model="openai/different-service")
        assert (
            resolve_openai_compatible("atlascloud", config, model_attr="recall_model")[
                2
            ]
            == model
        )

    @pytest.mark.parametrize("model", [None, "", "   ", 42])
    def test_resolve_uses_default_without_valid_model(self, monkeypatch, model):
        monkeypatch.delenv("RECALL_MODEL", raising=False)
        config = SimpleNamespace(recall_model=model)
        assert (
            resolve_openai_compatible("atlascloud", config, model_attr="recall_model")[
                2
            ]
            == ATLASCLOUD_DEFAULT_MODEL
        )

    def test_env_key(self, monkeypatch):
        monkeypatch.setenv("ATLASCLOUD_API_KEY", "apikey-from-env")
        assert _env_key(ProviderName.ATLASCLOUD) == "apikey-from-env"

    def test_env_key_missing(self, monkeypatch):
        monkeypatch.delenv("ATLASCLOUD_API_KEY", raising=False)
        assert _env_key(ProviderName.ATLASCLOUD) == ""

    def test_has_credentials_from_tenant_config(self):
        config = MagicMock()
        config.atlascloud_api_key = "apikey-tenant"
        assert has_credentials("atlascloud", config) is True

    def test_has_credentials_without_key(self):
        config = MagicMock()
        config.atlascloud_api_key = None
        assert has_credentials("atlascloud", config) is False

    def test_resolve_prefers_tenant_config(self, monkeypatch):
        monkeypatch.setenv("ATLASCLOUD_API_KEY", "apikey-from-env")
        config = MagicMock()
        config.atlascloud_api_key = "apikey-tenant"
        key, base_url, model = resolve_openai_compatible("atlascloud", config)
        assert key == "apikey-tenant"
        assert base_url == ATLASCLOUD_CHAT_BASE_URL
        assert model == ATLASCLOUD_DEFAULT_MODEL

    def test_resolve_falls_back_to_env(self, monkeypatch):
        monkeypatch.setenv("ATLASCLOUD_API_KEY", "apikey-from-env")
        key, base_url, _ = resolve_openai_compatible("atlascloud", None)
        assert key == "apikey-from-env"
        assert base_url == ATLASCLOUD_CHAT_BASE_URL

    def test_resolve_returns_empty_key_when_unset(self, monkeypatch):
        monkeypatch.delenv("ATLASCLOUD_API_KEY", raising=False)
        key, base_url, model = resolve_openai_compatible("atlascloud", None)
        assert key == ""
        # The endpoint and model are still reported so callers can log what
        # would have been used.
        assert base_url == ATLASCLOUD_CHAT_BASE_URL
        assert model == ATLASCLOUD_DEFAULT_MODEL


class TestOtherProvidersUnaffected:
    def test_openrouter_still_resolves(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        key, base_url, _ = resolve_openai_compatible("openrouter", None)
        assert key == "sk-or-test"
        assert base_url == "https://openrouter.ai/api/v1"

    def test_unknown_provider_returns_empty(self):
        assert resolve_openai_compatible("nope", None) == ("", "", "")
