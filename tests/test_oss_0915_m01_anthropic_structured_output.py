"""oss-0915-m-01 — ``anthropic`` structured output fails loudly, not silently.

``ProviderName.ANTHROPIC`` is served by ``OpenAILLMProvider`` against
Anthropic's OpenAI-compatible endpoint, which 400s on both ``response_format``
shapes ``complete_json`` sends (``json_object`` and non-strict
``json_schema``). Every enrichment / entity-extraction / contradiction call
therefore degraded to the fake provider while the write reported success.

Verified against the two documented upstream error messages, not a live call
(no LLM calls may be made from this suite). Under test:

* ``complete_json`` refuses for ``anthropic`` BEFORE any request is sent;
* ``complete_text`` (no ``response_format``) is unaffected;
* core-api refuses to start with ``ENTITY_EXTRACTION_PROVIDER=anthropic``;
* the fake-fallback warning names the primary provider.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from common.llm.providers.openai import (
    OpenAILLMProvider,
    UnsupportedStructuredOutputError,
)
from common.llm.retry import call_with_fallback


class _RecordingCompletions:
    def __init__(self, content: str = "{}") -> None:
        self.calls: list[dict] = []
        self._content = content

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content=self._content),
                )
            ]
        )


def _provider(name: str, content: str = "{}"):
    # Bypass ``__init__``: it builds a real httpx pool, which is irrelevant
    # here and breaks wherever the installed openai/httpx pair has drifted.
    p = OpenAILLMProvider.__new__(OpenAILLMProvider)
    p._model = "m"
    p._provider_name = name
    completions = _RecordingCompletions(content)
    p._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return p, completions


@pytest.mark.asyncio
@pytest.mark.parametrize("schema", [None, {"type": "object"}])
async def test_anthropic_complete_json_refuses_without_a_request(schema):
    p, completions = _provider("anthropic")
    with pytest.raises(UnsupportedStructuredOutputError, match="anthropic"):
        await p.complete_json("prompt", response_schema=schema)
    assert completions.calls == []


@pytest.mark.asyncio
async def test_other_openai_compatible_providers_still_send_the_request():
    for name in ("openai", "openrouter", "atlascloud"):
        p, completions = _provider(name, json.dumps({"ok": 1}))
        assert await p.complete_json("prompt") == {"ok": 1}
        assert len(completions.calls) == 1


@pytest.mark.asyncio
async def test_anthropic_complete_text_is_unaffected():
    p, completions = _provider("anthropic", "hello")
    assert await p.complete_text("prompt") == "hello"
    assert "response_format" not in completions.calls[0]


def test_core_api_refuses_anthropic_entity_extraction_provider():
    from core_api.config import Settings

    with pytest.raises(ValidationError, match="ENTITY_EXTRACTION_PROVIDER=anthropic"):
        Settings(_env_file=None, entity_extraction_provider="anthropic")


def test_core_api_accepts_supported_entity_extraction_providers():
    from core_api.config import Settings

    for name in ("openai", "openrouter", "gemini", "fake", "none"):
        assert (
            Settings(_env_file=None, entity_extraction_provider=name)
        ).entity_extraction_provider == name


@pytest.mark.asyncio
async def test_fake_fallback_warning_names_consumer_and_provider(caplog):
    p, completions = _provider("anthropic")

    async def call_fn(provider):
        return await provider.complete_json("prompt")

    with caplog.at_level(logging.WARNING, logger="common.llm.retry"):
        result = await call_with_fallback(
            "anthropic",
            call_fn,
            lambda: {"fake": True},
            service_label="entity-extraction",
            max_attempts=1,
            provider_factory=lambda *a, **k: p,
        )

    assert result == {"fake": True}
    assert completions.calls == []
    final = [r for r in caplog.records if "using fake fallback" in r.getMessage()]
    assert len(final) == 1
    assert final[0].levelno == logging.WARNING
    assert "entity-extraction" in final[0].getMessage()
    assert "'anthropic'" in final[0].getMessage()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc_type", "expected_attempts", "expected_sleeps"),
    [
        # Deterministic: one attempt, no backoff, even with no opt-in.
        (UnsupportedStructuredOutputError, 1, 0),
        # Control — proves the patched sleep is the one the loop uses.
        (RuntimeError, 3, 2),
    ],
)
async def test_unsupported_structured_output_is_never_retried(
    monkeypatch, exc_type, expected_attempts, expected_sleeps
):
    import common.llm.retry as retry_mod

    sleeps: list[float] = []

    async def _no_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(retry_mod.asyncio, "sleep", _no_sleep)
    attempts = 0

    async def _call():
        nonlocal attempts
        attempts += 1
        raise exc_type("boom")

    with pytest.raises(exc_type):
        await retry_mod.call_with_retry(_call, label="t", max_attempts=3)
    assert attempts == expected_attempts
    assert len(sleeps) == expected_sleeps
