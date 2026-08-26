"""Shared max-tokens truncation detection for JSON completions.

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
"""

from __future__ import annotations

_TRUNCATION_REASONS = ("MAX_TOKENS", "LENGTH")


def raise_if_truncated(
    response: object,
    *,
    provider: str,
    model: str,
    max_tokens: int,
) -> None:
    """Raise ``ValueError`` when *response* was cut at the output-token cap.

    Duck-typed over all three SDKs' response shapes (Gemini
    ``candidates`` / OpenAI ``choices``): any attribute missing or empty
    means "not truncated" — never break the happy path on an SDK shape
    change.
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
    if any(marker in name.upper() for marker in _TRUNCATION_REASONS):
        raise ValueError(
            f"{provider} model {model} JSON response truncated at "
            f"max_output_tokens={max_tokens} (runaway generation); "
            "failing fast instead of parsing partial JSON"
        )
