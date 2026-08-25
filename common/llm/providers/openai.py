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

from common.llm.call_context import llm_call_label
from common.llm.constants import (
    LLM_JSON_MAX_OUTPUT_TOKENS,
    LLM_PROVIDER_MAX_RETRIES,
    OPENAI_CHAT_BASE_URL,
    OPENAI_HOSTED_CHAT_BASE_URL,
    OPENAI_HTTPX_CONNECT_TIMEOUT_SECONDS,
    OPENAI_HTTPX_MAX_CONNECTIONS,
    OPENAI_HTTPX_MAX_KEEPALIVE_CONNECTIONS,
    OPENAI_HTTPX_POOL_TIMEOUT_SECONDS,
    OPENAI_REQUEST_TIMEOUT_SECONDS,
)
from common.llm.providers._shape_error import ProviderResponseShapeError
from common.llm.providers._truncation import raise_if_truncated

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


def _usage_tokens(response) -> tuple[int, int, int]:
    """``(prompt, completion, reasoning)`` token counts from a chat response.

    E3 — OpenAI bills tokens, not calls, so per-call cost has to be visible
    in the logs to make a spend fix verifiable in dollars. ``reasoning``
    (inside ``completion_tokens_details``, gpt-5-family reasoning models
    only) is broken out because it is billed as output while never
    appearing in the visible content — it is the invisible majority of the
    contradiction judge's spend. Every field is optional on OpenAI-
    compatible endpoints, so absence degrades to 0 rather than raising.
    """

    def _int(value: object) -> int:
        # Coerce defensively: OpenAI-compatible endpoints return None or
        # omit fields, and unit-test stubs return non-numeric attributes.
        # The ignore code is ``call-overload``, not ``arg-type``: ``int`` is
        # overloaded, so passing ``object`` fails overload resolution rather
        # than a single argument's type. It read ``arg-type`` until common/
        # entered the mypy gate, which is the first thing that ran mypy here
        # and reported the mismatch.
        try:
            return int(value)  # type: ignore[call-overload]
        except (TypeError, ValueError):
            return 0

    usage = getattr(response, "usage", None)
    details = getattr(usage, "completion_tokens_details", None)
    return (
        _int(getattr(usage, "prompt_tokens", 0)),
        _int(getattr(usage, "completion_tokens", 0)),
        _int(getattr(details, "reasoning_tokens", 0)),
    )


def _is_hosted_openai(base_url: str) -> bool:
    """True when ``base_url`` is api.openai.com itself, after any override."""
    return base_url.rstrip("/") == OPENAI_HOSTED_CHAT_BASE_URL


def _strict_schema(schema: dict) -> dict:
    """Return a copy of ``schema`` that strict JSON mode accepts.

    Strict mode, as OpenAI defines it and as Anthropic's OpenAI-compatible
    endpoint enforces it, wants every object closed
    (``additionalProperties: false``) with every property listed under
    ``required``. Pydantic-generated schemas leave both open for fields
    with defaults. This walks the schema (``properties``, ``items``,
    ``anyOf``/``oneOf``/``allOf``, ``$defs``) and closes each object. It
    also drops ``default`` and ``title``, which strict mode rejects or
    ignores and which carry no meaning for the model. The input is not
    modified.
    """
    if isinstance(schema, list):
        return [_strict_schema(s) for s in schema]  # type: ignore[return-value]
    if not isinstance(schema, dict):
        return schema
    out: dict = {}
    for key, value in schema.items():
        if key in ("default", "title"):
            continue
        if key in ("properties", "$defs"):
            out[key] = {k: _strict_schema(v) for k, v in value.items()}
        elif key in ("items", "anyOf", "oneOf", "allOf"):
            out[key] = _strict_schema(value)
        else:
            out[key] = value
    if out.get("type") == "object" or "properties" in out:
        out["additionalProperties"] = False
        out["required"] = list(out.get("properties", {}).keys())
    return out


def _strip_code_fence(content: str) -> str:
    """Remove a Markdown code fence around ``content`` when there is one.

    Without a ``response_format`` to constrain them, models often answer
    with the JSON wrapped in a fence. The JSON inside is what the caller
    asked for.
    """
    text = content.strip()
    if not text.startswith("```"):
        return content
    first_newline = text.find("\n")
    if first_newline == -1:
        return content
    body = text[first_newline + 1 :]
    if body.rstrip().endswith("```"):
        body = body.rstrip()[:-3]
    return body


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
        # concurrent writes x 10 enrichment calls per request = 160
        # concurrent LLM calls per worker process, with the next
        # tenant's traffic queueing at the pool layer. Sizing the pool
        # 2x the worst-case fan-out keeps headroom; values are env-
        # tunable for incident-time adjustment.
        #
        # ``max_retries`` is pinned rather than left at the SDK's default
        # 2, which put a silent 3x multiplier under ``call_with_retry``
        # AND under the per-request timeout that the inline and bulk
        # ceilings are derived from. See ``LLM_PROVIDER_MAX_RETRIES``.
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            max_retries=LLM_PROVIDER_MAX_RETRIES,
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
        reasoning_effort: str | None = None,
    ) -> dict:
        """Send a prompt and return a parsed JSON dict.

        The ``response_format`` sent depends on the endpoint, because the
        compatible servers do not agree on what they accept:

        - Hosted OpenAI (``OPENAI_HOSTED_CHAT_BASE_URL``) keeps the shapes
          it always got: ``json_object`` without a schema, and a
          non-strict ``json_schema`` with one.
        - Any other base URL (a self-hosted server, Anthropic's compatible
          endpoint, OpenRouter) gets no ``response_format`` without a
          schema, because LM Studio and Anthropic both reject
          ``json_object``; the prompt already asks for JSON and a code
          fence in the reply is stripped before parsing. With a schema it
          gets ``strict: true`` and a closed schema (see
          ``_strict_schema``), which is the one shape Anthropic accepts and
          which every other compatible server also takes.

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

        ``reasoning_effort`` (E3): forwarded to the chat completions API
        only when set, so callers doing bounded classification work (the
        contradiction judge) can bound hidden reasoning-token spend —
        billed as output, invisible in the content. Valid values are
        MODEL-SPECIFIC (gpt-5.4 family, wet-tested: ``"none"`` /
        ``"low"`` / ``"medium"`` / ``"high"`` / ``"xhigh"``); an
        unsupported value is a 400 on every call. ``None`` (the default)
        omits the parameter entirely, because non-reasoning models
        reject it with a 400. Sending it also drops ``temperature`` —
        see the inline note at the call.
        """
        t0 = time.perf_counter()
        hosted = _is_hosted_openai(self._base_url)
        create_kwargs: dict = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            # Runaway guard — same failure mode as the Gemini-backed
            # providers: an uncapped looping generation comes back as
            # truncated JSON (finish_reason="length").
            "max_completion_tokens": LLM_JSON_MAX_OUTPUT_TOKENS,
        }
        if response_schema is not None:
            create_kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "response",
                    "schema": (
                        response_schema if hosted else _strict_schema(response_schema)
                    ),
                    "strict": not hosted,
                },
            }
        elif hosted:
            create_kwargs["response_format"] = {"type": "json_object"}
        if seed is not None:
            create_kwargs["seed"] = seed
        if reasoning_effort is not None:
            create_kwargs["reasoning_effort"] = reasoning_effort
            # Wet-tested against gpt-5.4-nano: sending reasoning_effort
            # switches the model into reasoning mode, which rejects any
            # non-default temperature with a 400 ("only the default (1)
            # value is supported") — and call_with_fallback would swallow
            # that into the abstain fallback, silently disabling the
            # judge. Temperature is a no-op in reasoning mode anyway, so
            # drop it rather than fail the call.
            create_kwargs.pop("temperature", None)
        response = await self._client.chat.completions.create(**create_kwargs)
        llm_ms = int((time.perf_counter() - t0) * 1000)
        tokens_in, tokens_out, tokens_reasoning = _usage_tokens(response)
        logger.info(
            "OpenAI-compatible complete_json (%s) took %dms "
            "tokens_in=%d tokens_out=%d tokens_reasoning=%d service=%s",
            self._model,
            llm_ms,
            tokens_in,
            tokens_out,
            tokens_reasoning,
            llm_call_label.get() or "-",
        )
        content = response.choices[0].message.content
        if not content:
            raise ValueError(f"OpenAI returned empty content for model {self._model}")
        raise_if_truncated(
            response,
            provider="OpenAI-compatible",
            model=self._model,
            max_tokens=LLM_JSON_MAX_OUTPUT_TOKENS,
        )
        parsed = json.loads(_strip_code_fence(content))
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
        tokens_in, tokens_out, tokens_reasoning = _usage_tokens(response)
        logger.info(
            "OpenAI-compatible complete_text (%s) took %dms "
            "tokens_in=%d tokens_out=%d tokens_reasoning=%d service=%s",
            self._model,
            llm_ms,
            tokens_in,
            tokens_out,
            tokens_reasoning,
            llm_call_label.get() or "-",
        )
        return response.choices[0].message.content or ""
