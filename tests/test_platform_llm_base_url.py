"""``PLATFORM_LLM_BASE_URL`` points OpenAI chat calls at an OpenAI-compatible endpoint.

The OpenAI chat base URL was a constant, so no deployment could route chat
calls through a metering proxy, a gateway or a self-hosted model server, while
embeddings already could (``PLATFORM_EMBEDDING_BASE_URL``). Operator env only:
tenant config cannot set it.
"""

from unittest.mock import MagicMock

import pytest

from common.llm._credentials import resolve_openai_compatible
from common.llm.constants import OPENAI_CHAT_BASE_URL, openai_chat_base_url

pytestmark = pytest.mark.unit


def test_unset_keeps_openai(monkeypatch):
    monkeypatch.delenv("PLATFORM_LLM_BASE_URL", raising=False)
    assert openai_chat_base_url() == OPENAI_CHAT_BASE_URL == "https://api.openai.com/v1"


def test_blank_keeps_openai(monkeypatch):
    monkeypatch.setenv("PLATFORM_LLM_BASE_URL", "  ")
    assert openai_chat_base_url() == OPENAI_CHAT_BASE_URL


def test_set_is_used_without_trailing_slash(monkeypatch):
    monkeypatch.setenv("PLATFORM_LLM_BASE_URL", "http://127.0.0.1:8787/v1/")
    assert openai_chat_base_url() == "http://127.0.0.1:8787/v1"


def test_openai_resolution_follows_the_setting(monkeypatch):
    monkeypatch.setenv("PLATFORM_LLM_BASE_URL", "http://proxy.internal/v1")
    config = MagicMock()
    config.openai_api_key = "sk-test"
    _key, url, _model = resolve_openai_compatible("openai", config)
    assert url == "http://proxy.internal/v1"


def test_other_providers_are_not_redirected(monkeypatch):
    monkeypatch.setenv("PLATFORM_LLM_BASE_URL", "http://proxy.internal/v1")
    config = MagicMock()
    config.anthropic_api_key = "sk-ant"
    _key, url, _model = resolve_openai_compatible("anthropic", config)
    assert url != "http://proxy.internal/v1"
