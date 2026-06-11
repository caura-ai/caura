"""OpenAI-compatible LLM provider — moved from
``core_api.providers.openai_provider`` (CAURA-595).

Wraps the ``openai`` SDK (AsyncOpenAI) to implement the
``LLMProvider`` protocol. Supports OpenAI, Anthropic (via OpenAI-
compatible endpoint), and OpenRouter by varying the ``base_url``
parameter.

The previous ``settings.openai_request_timeout_seconds`` import has
been replaced with a constructor arg defaulting to
``OPENAI_REQUEST_TIMEOUT_SECONDS`` from ``common.llm.constants`` —
the registry passes the resolved value through. Same decoupling
shape Step B used for ``OpenAIEmbeddingProvider``.
"""

from __future__ import annotations

import json
import logging
import time

import httpx
import openai

from common.llm.constants import (
    OPENAI_CHAT_BASE_URL,
    OPENAI_HTTPX_CONNECT_TIMEOUT_SECONDS,
    OPENAI_HTTPX_MAX_CONNECTIONS,
    OPENAI_HTTPX_MAX_KEEPALIVE_CONNECTIONS,
    OPENAI_HTTPX_POOL_TIMEOUT_SECONDS,
    OPENAI_REQUEST_TIMEOUT_SECONDS,
)
from common.llm.providers._shape_error import ProviderResponseShapeError

logger = logging.getLogger(__name__)


# CAURA-651: same hazard as VertexResponseShapeError /
# GeminiResponseShapeError — OpenAI's structured-output mode doesn't
# universally constrain the top-level shape (especially via
# OpenAI-compatible endpoints), so a list (or other non-dict) can
# leak through and cause downstream ``.get(...)`` to raise bare
# AttributeError.
class OpenAIResponseShapeError(ProviderResponseShapeError):
    def __init__(self, content: str, parsed_type: str) -> None:
        super().__init__("OpenAI", content, parsed_type)

    def __reduce__(self) -> tuple:
        # See VertexResponseShapeError.__reduce__ for rationale.
        return (type(self), (self.args[1], self.args[2]))


class OpenAILLMProvider:
    """LLM provider using the OpenAI chat completions API.

    Works with any OpenAI-compatible endpoint (OpenAI, Anthropic, OpenRouter)
    by setting the appropriate ``base_url``.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = OPENAI_CHAT_BASE_URL,
        provider_name: str = "openai",
        request_timeout_seconds: float = OPENAI_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url
        self._provider_name = provider_name
        # Explicit per-call timeout — without this the SDK rides httpx's
        # default and a single hung upstream call would eat the whole
        # enrichment budget silently.
        #
        # Per-PHASE timeout rather than a bare float: a float keeps
        # httpx's default 5 s connect/pool phases, and on Cloud Run with
        # a VPC connector in ``all-traffic`` egress mode every outbound
        # call rides the connector + Cloud NAT — a cold connection
        # (first call after idle, drained keepalive pool, NAT state
        # churn) intermittently exceeds 5 s. Observed in prod as a
        # steady trickle of ``httpcore.ConnectTimeout`` from the
        # enrichment / entity-extraction handlers. ``read`` keeps the
        # full request budget (the provider's thinking time); only
        # connect/pool get cold-path headroom.
        #
        # Explicit ``http_client`` with ``httpx.Limits`` sized for our
        # bulk-write fan-out (CAURA-627). The SDK's default httpx pool
        # (100 max / 20 keepalive) saturates under storm load — 16
        # concurrent writes × 10 enrichment calls per request = 160
        # concurrent LLM calls per worker process, with the next
        # tenant's traffic queueing at the pool layer. Sizing the pool
        # 2x the worst-case fan-out keeps headroom; values are env-
        # tunable for incident-time adjustment.
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=httpx.Timeout(
                connect=OPENAI_HTTPX_CONNECT_TIMEOUT_SECONDS,
                # read AND write keep the full request budget — the bare
                # float this replaces set every phase to it, and large
                # prompt payloads can legitimately take >15 s to upload
                # on a slow uplink.
                read=request_timeout_seconds,
                write=request_timeout_seconds,
                # Pool tracks the request budget unless explicitly
                # overridden — ``is not None`` (not ``or``) so an
                # explicit 0.0 override means "don't wait", not "unset".
                pool=(
                    OPENAI_HTTPX_POOL_TIMEOUT_SECONDS
                    if OPENAI_HTTPX_POOL_TIMEOUT_SECONDS is not None
                    else request_timeout_seconds
                ),
            ),
            http_client=httpx.AsyncClient(
                limits=httpx.Limits(
                    max_connections=OPENAI_HTTPX_MAX_CONNECTIONS,
                    max_keepalive_connections=OPENAI_HTTPX_MAX_KEEPALIVE_CONNECTIONS,
                ),
            ),
        )

    @property
    def provider_name(self) -> str:
        return self._provider_name

    @property
    def model(self) -> str:
        return self._model

    async def aclose(self) -> None:
        """Close the underlying httpx pool cleanly.

        Without this, ``asyncio`` debug mode emits ``ResourceWarning:
        Unclosed <httpx.AsyncClient>`` when the provider is GC'd —
        noisy in tests and a leak in long-lived processes that rotate
        client instances. Idempotent; safe to call multiple times.
        """
        await self._client.close()

    async def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        seed: int | None = None,
        response_schema: dict | None = None,
    ) -> dict:
        """Send a prompt and return a parsed JSON dict.

        Without ``response_schema``, uses
        ``response_format={"type": "json_object"}`` to enforce shape-less
        JSON output (back-compat for enrichment and dedup callers).

        ``seed`` (A5a #2): when provided, forwarded to OpenAI's chat
        completions API for response determinism. ``temperature=0.0`` is
        not sufficient on its own — small models (gpt-class -nano) still
        sample non-deterministically without a seed. Callers that need
        repeatable output across retries (entity extraction, dedup
        disambiguation) should pass a stable seed derived from the
        prompt. Omit (or pass ``None``) for vanilla non-deterministic
        completion.

        ``response_schema`` (A5b #3): when provided, switches to
        ``response_format={"type": "json_schema", ...}`` so the API
        enforces the output shape server-side. ``strict=False`` —
        Pydantic-generated schemas don't always satisfy OpenAI's strict-
        mode requirements (additionalProperties=false everywhere); the
        client-side Pydantic parse is the real guardrail. Passing
        ``None`` preserves today's shape-less behaviour.
        """
        t0 = time.perf_counter()
        if response_schema is not None:
            response_format: dict = {
                "type": "json_schema",
                "json_schema": {
                    "name": "response",
                    "schema": response_schema,
                    "strict": False,
                },
            }
        else:
            response_format = {"type": "json_object"}
        create_kwargs: dict = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": response_format,
            "temperature": temperature,
        }
        if seed is not None:
            create_kwargs["seed"] = seed
        response = await self._client.chat.completions.create(**create_kwargs)
        llm_ms = int((time.perf_counter() - t0) * 1000)
        logger.info(
            "OpenAI-compatible complete_json (%s) took %dms",
            self._model,
            llm_ms,
        )
        content = response.choices[0].message.content
        if not content:
            raise ValueError(f"OpenAI returned empty content for model {self._model}")
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            raise OpenAIResponseShapeError(content, type(parsed).__name__)
        return parsed

    async def complete_text(
        self,
        prompt: str,
        *,
        temperature: float = 0.3,
        max_tokens: int = 1000,
    ) -> str:
        """Send a prompt and return the raw text content."""
        t0 = time.perf_counter()
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            max_completion_tokens=max_tokens,
        )
        llm_ms = int((time.perf_counter() - t0) * 1000)
        logger.info(
            "OpenAI-compatible complete_text (%s) took %dms",
            self._model,
            llm_ms,
        )
        return response.choices[0].message.content or ""
