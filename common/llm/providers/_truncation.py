"""Shared max-tokens truncation detection for JSON and text completions.

Every backend reports an output cut at the token ceiling on the first
result's ``finish_reason``: the Gemini SDKs (``vertexai.generative_models``
and ``google-genai``) put an enum named ``MAX_TOKENS`` on
``candidates[0]``, the OpenAI-compatible chat API (OpenAI / Anthropic /
OpenRouter) puts the string ``"length"`` on ``choices[0]``. A truncated
JSON body still parses *sometimes* (when the cut lands between values)
but usually raises a ``JSONDecodeError`` pointing at a huge character
offset — which reads like a model-output bug rather than what it is.
Detecting the finish reason before parsing turns that into a clear,
retryable error.

A truncated text body is worse, because nothing downstream fails: it is
returned as though it were the answer. Thinking and reasoning models make
it common, since their thoughts count against the same cap. With recall's
500-token budget, gemini-3.8-flash spent about 480 tokens thinking and
returned a 15-token fragment, finishing ``MAX_TOKENS``. Raising lets
``call_with_fallback`` try the next tier instead.
"""

from __future__ import annotations

from typing import Literal

_TRUNCATION_REASONS = ("MAX_TOKENS", "LENGTH")


def raise_if_truncated(
    response: object,
    *,
    provider: str,
    model: str,
    max_tokens: int,
    kind: Literal["JSON", "text"] = "JSON",
) -> None:
    """Raise ``ValueError`` when *response* was cut at the output-token cap.

    Duck-typed over all three SDKs' response shapes (Gemini
    ``candidates`` / OpenAI ``choices``): any attribute missing or empty
    means "not truncated" — never break the happy path on an SDK shape
    change. ``kind`` names what was cut, for the message.
    """
    results = (
        getattr(response, "candidates", None)
        or getattr(response, "choices", None)
        or []
    )
    if not results:
        return
    reason = getattr(results[0], "finish_reason", None)
    if reason is None:
        return
    name = getattr(reason, "name", None) or str(reason)
    if not any(marker in name.upper() for marker in _TRUNCATION_REASONS):
        return
    if kind == "JSON":
        raise ValueError(
            f"{provider} model {model} JSON response truncated at "
            f"max_output_tokens={max_tokens} (runaway generation); "
            "failing fast instead of parsing partial JSON"
        )
    raise ValueError(
        f"{provider} model {model} text response truncated at "
        f"max_output_tokens={max_tokens} (a thinking model's thoughts count "
        "against it); not returning a cut-off answer"
    )
