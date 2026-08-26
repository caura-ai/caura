"""Truncation guard for JSON completions (Vertex + Gemini providers).

Prod 2026-08-26 (Vertex-only cutover): gemini-2.5-flash-lite
occasionally emitted runaway ~50k-token JSON for entity extraction; the
API truncated it mid-string and ``complete_json`` surfaced a
``JSONDecodeError`` at a ~200KB char offset. Two-part fix under test:

* ``complete_json`` now passes ``max_output_tokens=LLM_JSON_MAX_OUTPUT_TOKENS``
  so a runaway fails fast and cheap;
* ``raise_if_truncated`` turns a ``finish_reason=MAX_TOKENS`` response
  into a clear, retryable ``ValueError`` *before* JSON parsing.
"""

from __future__ import annotations

import enum
import json
from types import SimpleNamespace

import pytest

from common.llm.constants import LLM_JSON_MAX_OUTPUT_TOKENS
from common.llm.providers._truncation import raise_if_truncated
from common.llm.providers.gemini import GeminiLLMProvider
from common.llm.providers.vertex import VertexLLMProvider


class _FinishReason(enum.Enum):
    """Stand-in for both SDKs' finish-reason enums (``.name`` is what counts)."""

    STOP = 1
    MAX_TOKENS = 2
    SAFETY = 3


def _response(text: str, finish_reason: object | None = _FinishReason.STOP):
    candidates = (
        [] if finish_reason is None else [SimpleNamespace(finish_reason=finish_reason)]
    )
    return SimpleNamespace(text=text, candidates=candidates)


# ---------------------------------------------------------------------------
# raise_if_truncated — duck-typed detection
# ---------------------------------------------------------------------------


class TestRaiseIfTruncated:
    def test_max_tokens_raises_with_context(self):
        with pytest.raises(ValueError) as exc:
            raise_if_truncated(
                _response('{"partial": "tru', _FinishReason.MAX_TOKENS),
                provider="Vertex",
                model="gemini-2.5-flash-lite",
                max_tokens=8192,
            )
        msg = str(exc.value)
        assert "truncated" in msg
        assert "max_output_tokens=8192" in msg
        assert "gemini-2.5-flash-lite" in msg

    def test_stop_does_not_raise(self):
        raise_if_truncated(
            _response("{}", _FinishReason.STOP),
            provider="Vertex",
            model="m",
            max_tokens=1,
        )

    def test_string_reason_matches(self):
        # google-genai can surface the reason as a plain string.
        raise_if_truncated(
            _response("{}", "STOP"), provider="Gemini", model="m", max_tokens=1
        )
        with pytest.raises(ValueError):
            raise_if_truncated(
                _response("{}", "MAX_TOKENS"),
                provider="Gemini",
                model="m",
                max_tokens=1,
            )

    def test_openai_choices_length_raises(self):
        # OpenAI-compatible shape: ``choices[0].finish_reason == "length"``.
        resp = SimpleNamespace(choices=[SimpleNamespace(finish_reason="length")])
        with pytest.raises(ValueError, match="truncated at max_output_tokens"):
            raise_if_truncated(
                resp, provider="OpenAI-compatible", model="gpt-5.4-nano", max_tokens=8192
            )

    def test_openai_choices_stop_does_not_raise(self):
        resp = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop")])
        raise_if_truncated(resp, provider="OpenAI-compatible", model="m", max_tokens=1)

    def test_missing_shapes_do_not_raise(self):
        # No candidates / no finish_reason / no attributes at all — an SDK
        # shape change must never break the happy path.
        raise_if_truncated(
            _response("{}", None), provider="Vertex", model="m", max_tokens=1
        )
        raise_if_truncated(object(), provider="Vertex", model="m", max_tokens=1)
        raise_if_truncated(
            SimpleNamespace(candidates=[SimpleNamespace()]),
            provider="Vertex",
            model="m",
            max_tokens=1,
        )


# ---------------------------------------------------------------------------
# VertexLLMProvider.complete_json
# ---------------------------------------------------------------------------


class _FakeGenerativeModel:
    """Captures the GenerationConfig and returns a canned response."""

    last_config = None
    canned_response = None

    def __init__(self, model_name):
        self.model_name = model_name

    def generate_content(self, prompt, generation_config=None):
        type(self).last_config = generation_config
        return type(self).canned_response


@pytest.fixture()
def _patched_vertex(monkeypatch):
    import vertexai.generative_models as gm

    monkeypatch.setattr(gm, "GenerativeModel", _FakeGenerativeModel)
    # aiplatform.init does network-free client setup, but stub it anyway
    # so tests never touch ADC.
    from google.cloud import aiplatform

    monkeypatch.setattr(aiplatform, "init", lambda **kw: None)
    _FakeGenerativeModel.last_config = None
    _FakeGenerativeModel.canned_response = None
    return _FakeGenerativeModel


class TestVertexCompleteJson:
    def _provider(self):
        return VertexLLMProvider(
            project_id="test-proj", location="us-central1", model="gemini-2.5-flash-lite"
        )

    @pytest.mark.asyncio
    async def test_happy_path_parses_and_caps_output(self, _patched_vertex):
        _patched_vertex.canned_response = _response(json.dumps({"ok": True}))
        result = await self._provider().complete_json("prompt")
        assert result == {"ok": True}
        cfg = _patched_vertex.last_config
        # vertexai's GenerationConfig keeps kwargs on a private raw config;
        # to_dict() is the stable public view across SDK versions.
        cfg_dict = cfg.to_dict() if hasattr(cfg, "to_dict") else vars(cfg)
        assert int(cfg_dict.get("max_output_tokens", 0)) == LLM_JSON_MAX_OUTPUT_TOKENS

    @pytest.mark.asyncio
    async def test_truncated_response_raises_clear_error(self, _patched_vertex):
        _patched_vertex.canned_response = _response(
            '{"entities": [{"name": "tru', _FinishReason.MAX_TOKENS
        )
        with pytest.raises(ValueError, match="truncated at max_output_tokens"):
            await self._provider().complete_json("prompt")

    @pytest.mark.asyncio
    async def test_untruncated_bad_json_still_json_error(self, _patched_vertex):
        # A genuine parse failure (finish_reason=STOP) must keep raising
        # JSONDecodeError — the truncation guard must not swallow it.
        _patched_vertex.canned_response = _response("not-json")
        with pytest.raises(json.JSONDecodeError):
            await self._provider().complete_json("prompt")


# ---------------------------------------------------------------------------
# GeminiLLMProvider.complete_json
# ---------------------------------------------------------------------------


class _FakeGenaiModels:
    def __init__(self):
        self.last_config = None
        self.canned_response = None

    def generate_content(self, *, model, contents, config):
        self.last_config = config
        return self.canned_response


class TestGeminiCompleteJson:
    def _provider(self):
        p = GeminiLLMProvider(api_key="AIza-test", model="gemini-2.5-flash-lite")
        p._client = SimpleNamespace(models=_FakeGenaiModels())
        return p

    @pytest.mark.asyncio
    async def test_happy_path_parses_and_caps_output(self):
        p = self._provider()
        p._client.models.canned_response = _response(json.dumps({"ok": 1}))
        assert await p.complete_json("prompt") == {"ok": 1}
        cfg = p._client.models.last_config
        assert cfg.max_output_tokens == LLM_JSON_MAX_OUTPUT_TOKENS

    @pytest.mark.asyncio
    async def test_truncated_response_raises_clear_error(self):
        p = self._provider()
        p._client.models.canned_response = _response(
            '{"partial": "tru', _FinishReason.MAX_TOKENS
        )
        with pytest.raises(ValueError, match="truncated at max_output_tokens"):
            await p.complete_json("prompt")


# ---------------------------------------------------------------------------
# OpenAILLMProvider.complete_json (OpenAI / Anthropic / OpenRouter)
# ---------------------------------------------------------------------------


def _openai_response(content: str | None, finish_reason: str = "stop"):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(content=content),
            )
        ]
    )


class _FakeCompletions:
    def __init__(self):
        self.last_kwargs = None
        self.canned_response = None

    async def create(self, **kwargs):
        self.last_kwargs = kwargs
        return self.canned_response


class TestOpenAICompleteJson:
    def _provider(self):
        from common.llm.providers.openai import OpenAILLMProvider

        p = OpenAILLMProvider(api_key="sk-test", model="gpt-5.4-nano")
        completions = _FakeCompletions()
        p._client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions),
            close=lambda: None,
        )
        return p, completions

    @pytest.mark.asyncio
    async def test_happy_path_parses_and_caps_output(self):
        p, completions = self._provider()
        completions.canned_response = _openai_response(json.dumps({"ok": 2}))
        assert await p.complete_json("prompt") == {"ok": 2}
        assert (
            completions.last_kwargs["max_completion_tokens"]
            == LLM_JSON_MAX_OUTPUT_TOKENS
        )

    @pytest.mark.asyncio
    async def test_truncated_response_raises_clear_error(self):
        p, completions = self._provider()
        completions.canned_response = _openai_response(
            '{"partial": "tru', finish_reason="length"
        )
        with pytest.raises(ValueError, match="truncated at max_output_tokens"):
            await p.complete_json("prompt")
