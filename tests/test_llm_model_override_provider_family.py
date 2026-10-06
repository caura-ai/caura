"""A tenant on Gemini / OpenRouter must not be sent the OpenAI default model id.

Every LLM consumer passes ``ResolvedConfig.<service>_model`` as
``model_override``, and that property falls back to the GLOBAL
``entity_extraction_model`` (``gpt-5.4-nano`` unless set) whatever provider the
tenant resolved to. The override bypassed the provider-family guard in
``_model_for_provider``, and the Gemini guard's own default came from the
``ENTITY_EXTRACTION_MODEL`` env var core-api bridges that same OpenAI id into.
So in a default deployment the earlier provider-aware-model fix never applied.

These run the resolution the way core-api does: the real ``ResolvedConfig``,
the bridged env var, and the override the call sites pass.
"""

from __future__ import annotations

import logging

import pytest

from common.llm import registry
from common.llm._credentials import resolve_gemini_config
from common.llm.constants import GEMINI_DEFAULT_MODEL, OPENROUTER_DEFAULT_MODEL
from common.provider_names import ProviderName

pytestmark = pytest.mark.unit


@pytest.fixture
def core_api_env(monkeypatch):
    """The default core-api shape: OpenAI entity model bridged into env."""
    from core_api.services import organization_settings as org

    monkeypatch.setenv("ENTITY_EXTRACTION_MODEL", "gpt-5.4-nano")
    monkeypatch.setattr(org.global_settings, "entity_extraction_model", "gpt-5.4-nano")
    monkeypatch.setattr(org.global_settings, "gemini_api_key", "gm-test")
    monkeypatch.setattr(org.global_settings, "openrouter_api_key", "or-test")
    registry._PROVIDER_CACHE.clear()
    yield org
    registry._PROVIDER_CACHE.clear()


@pytest.mark.parametrize("service", ["enrichment", "recall", "entity_extraction"])
def test_gemini_tenant_gets_a_gemini_model(core_api_env, service):
    cfg = core_api_env.ResolvedConfig({service: {"provider": "gemini"}})
    override = getattr(cfg, f"{service}_model")
    assert override == "gpt-5.4-nano"  # the shared fallback this is about

    provider = registry.get_llm_provider(
        ProviderName.GEMINI, cfg, model_override=override, model_attr=f"{service}_model"
    )

    assert provider.model == GEMINI_DEFAULT_MODEL


def test_openrouter_tenant_gets_an_openrouter_model(core_api_env):
    cfg = core_api_env.ResolvedConfig({"enrichment": {"provider": "openrouter"}})

    provider = registry.get_llm_provider(
        ProviderName.OPENROUTER, cfg, model_override=cfg.enrichment_model
    )

    assert provider.model == OPENROUTER_DEFAULT_MODEL


def test_the_gemini_default_ignores_a_bridged_openai_env_model(monkeypatch):
    monkeypatch.setenv("ENTITY_EXTRACTION_MODEL", "gpt-5.4-nano")

    _, model = resolve_gemini_config(None)

    assert model == GEMINI_DEFAULT_MODEL


def test_a_gemini_env_model_is_still_the_gemini_default(monkeypatch):
    monkeypatch.setenv("ENTITY_EXTRACTION_MODEL", "gemini-2.5-flash-lite")

    _, model = resolve_gemini_config(None)

    assert model == "gemini-2.5-flash-lite"


def test_an_explicit_same_family_override_is_honoured(core_api_env):
    cfg = core_api_env.ResolvedConfig(
        {"enrichment": {"provider": "gemini", "model": "gemini-2.0-flash"}}
    )

    provider = registry.get_llm_provider(
        ProviderName.GEMINI, cfg, model_override=cfg.enrichment_model
    )

    assert provider.model == "gemini-2.0-flash"


def test_an_openai_tenant_still_gets_its_override(core_api_env, monkeypatch):
    monkeypatch.setattr(core_api_env.global_settings, "openai_api_key", "sk-test")
    cfg = core_api_env.ResolvedConfig({"enrichment": {"provider": "openai"}})

    provider = registry.get_llm_provider(
        ProviderName.OPENAI, cfg, model_override=cfg.enrichment_model
    )

    assert provider.model == "gpt-5.4-nano"


def test_an_unrecognised_override_passes_through(core_api_env):
    cfg = core_api_env.ResolvedConfig({"enrichment": {"provider": "openrouter"}})

    provider = registry.get_llm_provider(
        ProviderName.OPENROUTER, cfg, model_override="mistralai/mistral-small"
    )

    assert provider.model == "mistralai/mistral-small"


def test_a_discarded_override_is_logged(core_api_env, caplog):
    cfg = core_api_env.ResolvedConfig({"recall": {"provider": "gemini"}})

    with caplog.at_level(logging.WARNING, logger="common.llm._credentials"):
        registry.get_llm_provider(
            ProviderName.GEMINI,
            cfg,
            model_override="gpt-5.4-nano",
            model_attr="recall_model",
        )

    assert "gpt-5.4-nano" in caplog.text
    assert GEMINI_DEFAULT_MODEL in caplog.text
