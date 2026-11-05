"""Runcrate provider wiring.

Runcrate serves open-weight models through an OpenAI-compatible API, so it
rides the same ``OpenAILLMProvider`` path as OpenRouter and Atlas Cloud. These
tests pin the pieces that make that work: the enum member, the constants,
tenant/env credential resolution, model-family handling and membership in the
OpenAI-compatible set.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from common.llm._credentials import (
    _env_key,
    model_override_for_provider,
    resolve_openai_compatible,
)
from common.llm.constants import RUNCRATE_CHAT_BASE_URL, RUNCRATE_DEFAULT_MODEL
from common.llm.registry import _OPENAI_COMPATIBLE
from common.provider_names import ProviderName

pytestmark = pytest.mark.unit


class TestRuncrateConstants:
    def test_provider_name_member(self):
        assert ProviderName.RUNCRATE == "runcrate"

    def test_base_url(self):
        assert RUNCRATE_CHAT_BASE_URL == "https://api.runcrate.ai/v1"

    def test_default_model(self):
        assert RUNCRATE_DEFAULT_MODEL  # non-empty

    def test_is_openai_compatible(self):
        assert ProviderName.RUNCRATE in _OPENAI_COMPATIBLE


class TestRuncrateCredentials:
    @pytest.mark.parametrize(
        "model_attr", ["enrichment_model", "contradiction_model", "recall_model"]
    )
    @pytest.mark.parametrize(
        "model", ["deepseek-ai/DeepSeek-V4.1-Flash", "zai-org/GLM-5.3"]
    )
    def test_resolve_preserves_tenant_model(self, monkeypatch, model_attr, model):
        monkeypatch.setenv(model_attr.upper(), "Qwen/env-fallback")
        config = SimpleNamespace(**{model_attr: model})
        assert (
            resolve_openai_compatible("runcrate", config, model_attr=model_attr)[2]
            == model
        )

    @pytest.mark.parametrize("model", ["gpt-5.4-nano", "claude-sonnet-4-6"])
    def test_resolve_ignores_other_family_model(self, model):
        config = SimpleNamespace(enrichment_model=model)
        assert (
            resolve_openai_compatible("runcrate", config)[2] == RUNCRATE_DEFAULT_MODEL
        )

    def test_model_override_from_other_family_uses_resolved_model(self):
        assert (
            model_override_for_provider(
                "runcrate", "gpt-5.4-nano", RUNCRATE_DEFAULT_MODEL
            )
            == RUNCRATE_DEFAULT_MODEL
        )
        assert (
            model_override_for_provider(
                "runcrate", "zai-org/GLM-5.3", RUNCRATE_DEFAULT_MODEL
            )
            == "zai-org/GLM-5.3"
        )

    @pytest.mark.parametrize("model", [None, "", "   ", 42])
    def test_resolve_uses_default_without_valid_model(self, monkeypatch, model):
        monkeypatch.delenv("RECALL_MODEL", raising=False)
        config = SimpleNamespace(recall_model=model)
        assert (
            resolve_openai_compatible("runcrate", config, model_attr="recall_model")[2]
            == RUNCRATE_DEFAULT_MODEL
        )

    def test_env_key(self, monkeypatch):
        monkeypatch.setenv("RUNCRATE_API_KEY", "apikey-from-env")
        assert _env_key(ProviderName.RUNCRATE) == "apikey-from-env"

    def test_env_key_missing(self, monkeypatch):
        monkeypatch.delenv("RUNCRATE_API_KEY", raising=False)
        assert _env_key(ProviderName.RUNCRATE) == ""

    def test_resolve_prefers_tenant_config(self, monkeypatch):
        monkeypatch.setenv("RUNCRATE_API_KEY", "apikey-from-env")
        config = MagicMock()
        config.runcrate_api_key = "apikey-tenant"
        config.enrichment_model = None
        key, base_url, model = resolve_openai_compatible("runcrate", config)
        assert key == "apikey-tenant"
        assert base_url == RUNCRATE_CHAT_BASE_URL
        assert model == RUNCRATE_DEFAULT_MODEL

    def test_resolve_falls_back_to_env(self, monkeypatch):
        monkeypatch.setenv("RUNCRATE_API_KEY", "apikey-from-env")
        key, base_url, _ = resolve_openai_compatible("runcrate", None)
        assert key == "apikey-from-env"
        assert base_url == RUNCRATE_CHAT_BASE_URL

    def test_resolve_returns_empty_key_when_unset(self, monkeypatch):
        monkeypatch.delenv("RUNCRATE_API_KEY", raising=False)
        key, base_url, model = resolve_openai_compatible("runcrate", None)
        assert key == ""
        assert base_url == RUNCRATE_CHAT_BASE_URL
        assert model == RUNCRATE_DEFAULT_MODEL
