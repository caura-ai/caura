"""Truncation guard for text completions (Vertex, Gemini, OpenAI-compatible).

``complete_text`` returned whatever came back, so a reply cut at
``max_tokens`` was handed on as though it were the answer. Thinking and
reasoning models make that common, because their thoughts count against the
same cap. On recall's 500-token budget gemini-3.8-flash spent about 480
tokens thinking, returned a fragment such as ``**Relevant Facts:**`` plus
half a line, and finished ``MAX_TOKENS``. Recall served that fragment as the
summary.

These pin that each provider raises on a reported cut, and that recall then
falls back instead of serving the fragment.
"""

from __future__ import annotations

import enum
from types import SimpleNamespace

import pytest

import common.llm.registry as registry_mod
from common.llm.providers.gemini import GeminiLLMProvider
from common.llm.providers.openai import OpenAILLMProvider
from common.llm.providers.vertex import VertexLLMProvider
from core_api.constants import MEMORY_RECALL_SUMMARY_MAX_TOKENS
from core_api.services.recall_service import summarize_memories

pytestmark = [pytest.mark.unit]

FRAGMENT = "**Relevant Facts:**\n- Memory [1] (recorded 2026-"


class _FinishReason(enum.Enum):
    """Stand-in for both genai SDKs' finish-reason enums (``.name`` is what counts)."""

    STOP = 1
    MAX_TOKENS = 2


def _genai_response(text: str | None, finish_reason: object = _FinishReason.STOP):
    return SimpleNamespace(
        text=text, candidates=[SimpleNamespace(finish_reason=finish_reason)]
    )


class _FakeGenaiModels:
    def __init__(self, response: object) -> None:
        self.response = response
        self.last_config = None

    def generate_content(self, *, model, contents, config):
        self.last_config = config
        return self.response


def _vertex(response: object, model: str = "gemini-3.8-flash") -> VertexLLMProvider:
    p = VertexLLMProvider(project_id="test-proj", location="us", model=model)
    p._client = SimpleNamespace(models=_FakeGenaiModels(response))
    return p


def _gemini(response: object) -> GeminiLLMProvider:
    p = GeminiLLMProvider(api_key="AIza-test", model="gemini-3.8-flash")
    p._client = SimpleNamespace(models=_FakeGenaiModels(response))
    return p


class _FakeCompletions:
    def __init__(self, content: str | None, finish_reason: str) -> None:
        self.response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason=finish_reason,
                    message=SimpleNamespace(content=content),
                )
            ]
        )

    async def create(self, **kwargs: object) -> object:
        return self.response


def _openai(content: str | None, finish_reason: str) -> OpenAILLMProvider:
    p = OpenAILLMProvider(api_key="sk-test", model="gpt-5.4-nano")
    p._client = SimpleNamespace(
        chat=SimpleNamespace(completions=_FakeCompletions(content, finish_reason)),
        close=lambda: None,
    )
    return p


@pytest.mark.asyncio
@pytest.mark.parametrize("build", [_vertex, _gemini], ids=["vertex", "gemini"])
async def test_a_genai_reply_cut_at_the_cap_raises(build) -> None:
    p = build(_genai_response(FRAGMENT, _FinishReason.MAX_TOKENS))
    with pytest.raises(
        ValueError, match="text response truncated at max_output_tokens=500"
    ):
        await p.complete_text("prompt", max_tokens=500)


@pytest.mark.asyncio
@pytest.mark.parametrize("build", [_vertex, _gemini], ids=["vertex", "gemini"])
async def test_a_genai_reply_that_finished_is_returned(build) -> None:
    p = build(_genai_response("**Answer:** month-end"))
    assert await p.complete_text("prompt", max_tokens=500) == "**Answer:** month-end"
    assert p._client.models.last_config.max_output_tokens == 500


@pytest.mark.asyncio
async def test_a_reply_that_is_all_thought_raises_rather_than_returning_empty() -> None:
    # The whole budget went to thinking: no visible text at all.
    p = _vertex(_genai_response(None, _FinishReason.MAX_TOKENS))
    with pytest.raises(ValueError, match="truncated"):
        await p.complete_text("prompt", max_tokens=500)


@pytest.mark.asyncio
async def test_an_openai_reply_cut_at_the_cap_raises() -> None:
    # Reasoning models (gpt-5.4-nano/-mini) count reasoning tokens against
    # ``max_completion_tokens`` and finish ``length``.
    with pytest.raises(ValueError, match="text response truncated"):
        await _openai(FRAGMENT, "length").complete_text("prompt", max_tokens=500)


@pytest.mark.asyncio
async def test_an_openai_reply_that_finished_is_returned() -> None:
    assert (
        await _openai("done", "stop").complete_text("prompt", max_tokens=500) == "done"
    )


class _Mem(SimpleNamespace):
    def model_dump(self, mode: str | None = None) -> dict:
        return {"content": self.content, "memory_type": self.memory_type}


@pytest.mark.asyncio
async def test_recall_falls_back_instead_of_serving_a_cut_off_brief(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The platform LLM answers a keyless tenant (see
    # test_recall_diagnostic_answered_by.py); here it is a real Vertex provider
    # whose reply was cut at recall's cap.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    llm = _vertex(_genai_response(FRAGMENT, _FinishReason.MAX_TOKENS))
    monkeypatch.setattr(registry_mod, "get_platform_llm", lambda: llm)
    config = SimpleNamespace(
        recall_enabled=True,
        recall_provider="openai",
        recall_model="gpt-5.4-nano",
        openai_api_key=None,
    )
    mem = _Mem(
        content="The Granite Q4 review is scheduled for month-end.",
        memory_type="fact",
        title=None,
        status="active",
        ts_valid_start=None,
    )

    result = await summarize_memories(
        [mem], "When is the Granite Q4 review?", config, diagnostic=True
    )

    assert (
        llm._client.models.last_config.max_output_tokens
        == MEMORY_RECALL_SUMMARY_MAX_TOKENS
    )
    assert FRAGMENT not in result["summary"]
    assert result["summary"].startswith("(LLM unavailable;")
    assert result["diagnostic"]["recall_provider"] == "fake"
