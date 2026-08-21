"""One LLM attempt must be one HTTP request.

``openai.AsyncOpenAI`` defaults to ``max_retries=2`` — three requests per
call — and the LLM path stacks two retry layers of its own on top:
``call_with_fallback`` tries two providers, each through
``call_with_retry`` at ``LLM_RETRY_ATTEMPTS``. Unpinned, the real count
was the product: up to 12 requests where the code reads as 4.

It could not be caught at any layer above. SDK retries emit no log line,
so ``call_with_retry``'s "attempt 1/2 failed" covered up to three real
requests, and every stat and dashboard counted one.

These tests assert at the HTTP boundary for that exact reason. Mocking
the provider would prove nothing — the multiplier lives *below* it, which
is what made it invisible in the first place.

Two consequences are pinned here rather than just the constant:

  1. ``max_attempts=1`` means one request. ``recall_service`` passes it to
     fail fast to the fallback provider instead of retrying a slow
     primary; the SDK was overriding that with three tries and
     exponential backoff inside a 15 s budget.
  2. The retry budget is a sum, not a product. ``LLM_RETRY_ATTEMPTS``
     attempts is ``LLM_RETRY_ATTEMPTS`` requests.
"""

from __future__ import annotations

import httpx
import openai
import pytest

from common.llm.constants import LLM_PROVIDER_MAX_RETRIES, LLM_RETRY_ATTEMPTS
from common.llm.providers.openai import OpenAILLMProvider
from common.llm.retry import call_with_retry


def _counting_provider(calls: list[str]) -> OpenAILLMProvider:
    """A real provider whose only stub is the socket underneath it.

    The provider is built by its own constructor — that is the thing under
    test, since ``max_retries`` is what the constructor passes — and only
    the transport at the very bottom is swapped. Patching
    ``httpx.AsyncClient`` instead would be global (the module does ``import
    httpx``, so there is no module-local name to patch) and breaks the
    SDK's own ``isinstance(http_client, httpx.AsyncClient)`` check.

    Every request answers 429. That status is the one that matters: it is
    retryable to the SDK, so an unpinned client walks its whole ladder,
    where a 400 would exit on the first response and hide the defect.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(429, json={"error": {"message": "slow down"}})

    provider = OpenAILLMProvider(api_key="test-key", model="test-model")
    provider._client._client._transport = httpx.MockTransport(_handler)
    return provider


@pytest.fixture
def count_requests():
    """Collects one entry per HTTP request the SDK actually issues."""
    return []


@pytest.mark.unit
class TestOneAttemptIsOneRequest:
    @pytest.mark.asyncio
    async def test_a_single_call_issues_a_single_request(self, count_requests):
        provider = _counting_provider(count_requests)
        try:
            with pytest.raises(openai.RateLimitError):
                await provider.complete_text("hello")
        finally:
            await provider.aclose()

        assert len(count_requests) == 1

    @pytest.mark.asyncio
    async def test_max_attempts_one_means_one_request(self, count_requests):
        """The opt-out has to actually opt out.

        ``recall_service`` sets ``max_attempts=1`` on a latency-sensitive
        read path specifically so a slow primary is abandoned for the
        fallback provider rather than retried. Three hidden tries with
        exponential backoff is the opposite of what it asked for, inside
        a budget chosen to make the fast failure possible.
        """
        provider = _counting_provider(count_requests)
        try:
            with pytest.raises(openai.RateLimitError):
                await call_with_retry(
                    lambda: provider.complete_text("hello"),
                    label="test-single-attempt",
                    max_attempts=1,
                )
        finally:
            await provider.aclose()

        assert len(count_requests) == 1

    @pytest.mark.asyncio
    async def test_the_retry_budget_is_a_sum_not_a_product(
        self, count_requests, monkeypatch
    ):
        """N attempts must cost N requests, not N x 3."""
        monkeypatch.setattr("common.llm.retry.LLM_RETRY_DELAY_S", 0.0)
        provider = _counting_provider(count_requests)
        try:
            with pytest.raises(openai.RateLimitError):
                await call_with_retry(
                    lambda: provider.complete_text("hello"),
                    label="test-full-budget",
                    max_attempts=LLM_RETRY_ATTEMPTS,
                    base_delay=0.0,
                )
        finally:
            await provider.aclose()

        assert len(count_requests) == LLM_RETRY_ATTEMPTS


@pytest.mark.unit
class TestTheKnobIsPinned:
    @pytest.mark.asyncio
    async def test_client_carries_the_pinned_value(self):
        """Guard the library default, which returns on its own.

        It comes back the moment the constructor stops passing the value —
        an SDK upgrade, a refactor, a copied kwargs dict — and nothing
        downstream can see it happen.
        """
        provider = OpenAILLMProvider(api_key="test-key", model="test-model")
        try:
            assert provider._client.max_retries == LLM_PROVIDER_MAX_RETRIES
            assert provider._client.max_retries == 0, (
                "one attempt must be one request; the SDK's default 2 puts a "
                "silent 3x under call_with_retry and under the per-request "
                "timeout the inline/bulk ceilings are derived from"
            )
        finally:
            await provider.aclose()
