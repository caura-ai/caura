"""Token counts from a google-genai response, shared by the Gemini and Vertex providers.

``usage_metadata`` carries ``prompt_token_count``, ``candidates_token_count``
and, for thinking models, ``thoughts_token_count`` (billed as output, never
in the visible text). Every field is optional, so absence reads as 0.
"""

from __future__ import annotations

from common.usage import record_model_call


def _int(value: object) -> int:
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 0


def genai_usage_tokens(response: object) -> tuple[int, int, int]:
    """``(prompt, output, thoughts)`` token counts."""
    meta = getattr(response, "usage_metadata", None)
    if meta is None:
        return 0, 0, 0
    return (
        _int(getattr(meta, "prompt_token_count", 0)),
        _int(getattr(meta, "candidates_token_count", 0)),
        _int(getattr(meta, "thoughts_token_count", 0)),
    )


def record_genai_call(response: object, *, provider: str, model: str) -> None:
    """Record a billed google-genai call under the current usage scope."""
    tokens_in, tokens_out, tokens_reasoning = genai_usage_tokens(response)
    record_model_call(
        kind="llm",
        provider=provider,
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        tokens_reasoning=tokens_reasoning,
    )
