"""E4-prep — the per-call token log carries the service label.

The E3 token log made per-call cost visible; attributing it to a SERVICE
(judge vs enrichment vs dedup) still required correlating adjacent log
lines. The retry layer now publishes its per-tier label through
``common.llm.call_context.llm_call_label`` (a ContextVar riding the await
chain), and the OpenAI provider renders it as ``service=<label>`` on the
same log line — making a per-service token aggregation a one-regex
log-based metric.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.asyncio


def _response() -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))]
    )


async def test_direct_provider_call_logs_placeholder(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Outside the retry layer there is no ambient label — the field is
    still present (stable log shape for the metric regex) as ``service=-``."""
    from common.llm.providers.openai import OpenAILLMProvider

    provider = OpenAILLMProvider(api_key="sk-test", model="gpt-test")
    with (
        caplog.at_level(logging.INFO, logger="common.llm.providers.openai"),
        patch.object(
            provider._client.chat.completions,
            "create",
            AsyncMock(return_value=_response()),
        ),
    ):
        await provider.complete_json("test prompt")

    assert "service=-" in caplog.text


async def test_label_flows_from_call_with_fallback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``call_with_fallback(service_label=...)`` reaches the provider's
    token log line, tier-suffixed, with zero call-site plumbing."""
    from common.llm.providers.openai import OpenAILLMProvider
    from common.llm.retry import call_with_fallback

    provider = OpenAILLMProvider(api_key="sk-test", model="gpt-test")
    with (
        caplog.at_level(logging.INFO, logger="common.llm.providers.openai"),
        patch.object(
            provider._client.chat.completions,
            "create",
            AsyncMock(return_value=_response()),
        ),
    ):
        result = await call_with_fallback(
            primary_provider_name="openai",
            call_fn=lambda llm: llm.complete_json("test prompt"),
            fake_fn=lambda: {},
            provider_factory=lambda *a, **k: provider,
            service_label="contradiction_batch",
        )

    assert result == {"ok": True}
    assert "service=contradiction_batch-primary" in caplog.text


async def test_label_resets_after_the_call(caplog: pytest.LogCaptureFixture) -> None:
    """The ContextVar must not leak: a later unlabeled call in the same
    task logs the placeholder again, not the previous service's label."""
    from common.llm.call_context import llm_call_label
    from common.llm.providers.openai import OpenAILLMProvider
    from common.llm.retry import call_with_retry

    provider = OpenAILLMProvider(api_key="sk-test", model="gpt-test")
    with patch.object(
        provider._client.chat.completions, "create", AsyncMock(return_value=_response())
    ):
        await call_with_retry(
            lambda: provider.complete_json("p"), label="dedup-primary"
        )
        assert llm_call_label.get() == ""

        caplog.clear()
        with caplog.at_level(logging.INFO, logger="common.llm.providers.openai"):
            await provider.complete_json("p")

    assert "service=dedup-primary" not in caplog.text
    assert "service=-" in caplog.text


async def test_label_resets_even_when_the_call_raises() -> None:
    from common.llm.call_context import llm_call_label
    from common.llm.retry import call_with_retry

    async def _boom() -> dict:
        raise RuntimeError("provider down")

    with pytest.raises(RuntimeError):
        await call_with_retry(_boom, label="enrich-primary", max_attempts=1)

    assert llm_call_label.get() == ""
