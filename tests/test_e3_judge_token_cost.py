"""E3 — contradiction-judge LLM cost: token-usage logging + reasoning effort.

Aug-2026 finding: ~80% of prod OpenAI spend was OUTPUT tokens on the
contradiction judge, and on gpt-5-family reasoning models those are
dominated by hidden reasoning tokens — invisible in the logs (only
``llm_ms`` was recorded) and unaffected by the A61 call batching, which
is why the bill never moved. Two changes are pinned here:

1. **``complete_json``/``complete_text`` log ``response.usage``** —
   ``tokens_in`` / ``tokens_out`` / ``tokens_reasoning`` — so a spend fix
   is verifiable in dollars, not call counts.
2. **``complete_json`` accepts and forwards ``reasoning_effort``**, and
   every contradiction-judge call site passes
   ``settings.contradiction_reasoning_effort`` through
   ``_judge_effort_kwargs()`` — EMPTY kwargs when unset, so the wire
   request (and any non-reasoning model) is untouched by default.

Deliberately NOT changed (decision 2026-08-24): the 20-candidate
``/similar-candidates`` default stays — contradiction recall is already
the weak spot, and cutting candidates trades recall for cost.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.asyncio


def _response(
    content: str = '{"ok": true}', usage: object | None = None
) -> SimpleNamespace:
    """A chat-completions response stub. Built on SimpleNamespace, not
    MagicMock, so an absent ``usage`` is genuinely absent (getattr → None)
    rather than auto-created."""
    resp = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )
    if usage is not None:
        resp.usage = usage
    return resp


def _usage(
    prompt: int, completion: int, reasoning: int | None = None
) -> SimpleNamespace:
    details = (
        SimpleNamespace(reasoning_tokens=reasoning) if reasoning is not None else None
    )
    return SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        completion_tokens_details=details,
    )


# ---------------------------------------------------------------------------
# 1 — reasoning_effort forwarding through OpenAILLMProvider.complete_json
# ---------------------------------------------------------------------------


async def test_complete_json_forwards_reasoning_effort_when_provided() -> None:
    from common.llm.providers.openai import OpenAILLMProvider

    provider = OpenAILLMProvider(api_key="sk-test", model="gpt-test")
    mock_create = AsyncMock(return_value=_response())
    with patch.object(provider._client.chat.completions, "create", mock_create):
        await provider.complete_json("test prompt", reasoning_effort="low")

    kwargs = mock_create.call_args.kwargs
    assert kwargs.get("reasoning_effort") == "low", (
        f"Expected reasoning_effort='low' forwarded to OpenAI client, got kwargs={list(kwargs.keys())}"
    )
    # Wet-tested against gpt-5.4-nano: reasoning mode 400s on any
    # non-default temperature, so the two must never travel together.
    assert "temperature" not in kwargs, (
        "temperature must be dropped when reasoning_effort is sent — reasoning "
        "models reject non-default values with a 400"
    )


async def test_complete_json_omits_reasoning_effort_when_none() -> None:
    """Non-reasoning models reject the parameter with a 400, and
    ``call_with_fallback`` would read that as a provider outage — so an
    unset knob must not appear in the request at all."""
    from common.llm.providers.openai import OpenAILLMProvider

    provider = OpenAILLMProvider(api_key="sk-test", model="gpt-test")
    mock_create = AsyncMock(return_value=_response())
    with patch.object(provider._client.chat.completions, "create", mock_create):
        await provider.complete_json("test prompt")

    assert "reasoning_effort" not in mock_create.call_args.kwargs
    # ...and the temperature-drop is scoped to reasoning mode only: the
    # deterministic default (0.0) still travels on ordinary calls.
    assert mock_create.call_args.kwargs.get("temperature") == 0.0


# ---------------------------------------------------------------------------
# 2 — token-usage logging
# ---------------------------------------------------------------------------


async def test_complete_json_logs_token_usage(caplog: pytest.LogCaptureFixture) -> None:
    from common.llm.providers.openai import OpenAILLMProvider

    provider = OpenAILLMProvider(api_key="sk-test", model="gpt-test")
    mock_create = AsyncMock(
        return_value=_response(usage=_usage(120, 950, reasoning=900))
    )
    with (
        caplog.at_level(logging.INFO, logger="common.llm.providers.openai"),
        patch.object(provider._client.chat.completions, "create", mock_create),
    ):
        await provider.complete_json("test prompt")

    assert "tokens_in=120 tokens_out=950 tokens_reasoning=900" in caplog.text


async def test_complete_text_logs_token_usage(caplog: pytest.LogCaptureFixture) -> None:
    from common.llm.providers.openai import OpenAILLMProvider

    provider = OpenAILLMProvider(api_key="sk-test", model="gpt-test")
    mock_create = AsyncMock(
        return_value=_response(content="plain text", usage=_usage(10, 20))
    )
    with (
        caplog.at_level(logging.INFO, logger="common.llm.providers.openai"),
        patch.object(provider._client.chat.completions, "create", mock_create),
    ):
        await provider.complete_text("test prompt")

    assert "tokens_in=10 tokens_out=20 tokens_reasoning=0" in caplog.text


async def test_complete_json_tolerates_missing_usage(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """OpenAI-compatible endpoints (OpenRouter, self-hosted) may omit
    ``usage`` entirely; the call must succeed and log zeros."""
    from common.llm.providers.openai import OpenAILLMProvider

    provider = OpenAILLMProvider(api_key="sk-test", model="gpt-test")
    mock_create = AsyncMock(return_value=_response())  # no usage attr at all
    with (
        caplog.at_level(logging.INFO, logger="common.llm.providers.openai"),
        patch.object(provider._client.chat.completions, "create", mock_create),
    ):
        result = await provider.complete_json("test prompt")

    assert result == {"ok": True}
    assert "tokens_in=0 tokens_out=0 tokens_reasoning=0" in caplog.text


async def test_usage_tokens_tolerates_non_numeric_stub() -> None:
    """A stub response with non-numeric fields (the shape older tests
    use) must degrade to real ints, never leak a non-int into ``%d`` log
    formatting or raise. (A bare MagicMock coerces via ``__int__`` — the
    interesting case is an attribute int() genuinely rejects.)"""
    from common.llm.providers.openai import _usage_tokens

    assert all(type(v) is int for v in _usage_tokens(MagicMock()))
    stub = SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens="not-a-number",
            completion_tokens=None,
            completion_tokens_details=None,
        )
    )
    assert _usage_tokens(stub) == (0, 0, 0)


# ---------------------------------------------------------------------------
# 3 — the judge passes settings.contradiction_reasoning_effort through
# ---------------------------------------------------------------------------


async def test_judge_effort_kwargs_empty_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core_api.config import settings
    from core_api.services.contradiction_detector import _judge_effort_kwargs

    monkeypatch.setattr(settings, "contradiction_reasoning_effort", None)
    assert _judge_effort_kwargs() == {}


async def test_judge_effort_kwargs_set(monkeypatch: pytest.MonkeyPatch) -> None:
    from core_api.config import settings
    from core_api.services.contradiction_detector import _judge_effort_kwargs

    monkeypatch.setattr(settings, "contradiction_reasoning_effort", "minimal")
    assert _judge_effort_kwargs() == {"reasoning_effort": "minimal"}


async def test_batch_judge_forwards_effort_to_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end through ``call_with_fallback``: the batched judge's
    ``complete_json`` call carries the configured effort."""
    from core_api.config import settings
    from core_api.services.contradiction_detector import _llm_contradiction_check_batch

    monkeypatch.setattr(settings, "contradiction_reasoning_effort", "low")
    monkeypatch.setattr(settings, "entity_extraction_provider", "openai")

    seen_kwargs: dict = {}

    class _StubProvider:
        provider_name = "openai"
        model = "gpt-test"

        async def complete_json(self, prompt: str, **kwargs) -> dict:
            seen_kwargs.update(kwargs)
            return {"0": {"same_subject": False, "contradicts": False}}

    with patch("common.llm.registry.get_llm_provider", return_value=_StubProvider()):
        judged = await _llm_contradiction_check_batch(
            "new statement",
            [
                {"id": "c0", "content": "old statement"},
                {"id": "c1", "content": "other"},
            ],
            tenant_config=None,
        )

    assert seen_kwargs.get("reasoning_effort") == "low"
    assert len(judged) == 2


async def test_batch_judge_omits_effort_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the knob unset the judge must not pass the kwarg at all —
    test doubles and providers predating the parameter keep working."""
    from core_api.config import settings
    from core_api.services.contradiction_detector import _llm_contradiction_check_batch

    monkeypatch.setattr(settings, "contradiction_reasoning_effort", None)
    monkeypatch.setattr(settings, "entity_extraction_provider", "openai")

    class _LegacyProvider:
        provider_name = "openai"
        model = "gpt-test"

        # Deliberately NO **kwargs: passing reasoning_effort here raises
        # TypeError, which call_with_fallback would swallow into the
        # fake fallback (audit C1) — this pins that we never send it unset.
        async def complete_json(self, prompt: str) -> dict:
            return {"0": {"same_subject": False, "contradicts": False}}

    with patch("common.llm.registry.get_llm_provider", return_value=_LegacyProvider()):
        judged = await _llm_contradiction_check_batch(
            "new statement",
            [
                {"id": "c0", "content": "old statement"},
                {"id": "c1", "content": "other"},
            ],
            tenant_config=None,
        )

    assert judged == [
        {"same_subject": False, "contradicts": False},
        {"contradicts": False},
    ]
