"""Ambient label for the LLM call currently in flight (E4-prep).

The per-call token log (E3, ``common/llm/providers/openai.py``) is the
cost-attribution surface — but the provider is invoked through ``call_fn``
closures built by ``call_with_fallback`` and never sees the caller's
``service_label``. Threading a parameter through every provider method,
protocol, and test double would touch dozens of signatures for one log
field; a ``ContextVar`` instead rides the await chain from the retry
layer down to the provider with no call-site changes.

Set/reset by ``common.llm.retry.call_with_retry`` (which every production
LLM call goes through, primary and fallback tiers alike), read at log
time by the provider. A call that bypasses the retry layer (unit tests,
one-off scripts) logs the empty default, rendered as ``service=-``.

Lives in its own module so both ``retry`` and the providers can import it
without creating a cycle.
"""

from __future__ import annotations

from contextvars import ContextVar

# The retry layer stores its per-tier label here, e.g.
# ``"contradiction_batch-primary"`` / ``"contradiction_batch-fallback"`` —
# the tier suffix is kept deliberately so a cost regression can be
# attributed not just to a service but to its fallback tier misfiring.
llm_call_label: ContextVar[str] = ContextVar("llm_call_label", default="")
