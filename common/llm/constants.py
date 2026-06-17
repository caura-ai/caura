"""LLM-provider constants — moved from ``core_api.constants`` (CAURA-595).

Mirrors ``common/embedding/constants.py``. core-api's ``constants.py``
keeps re-exports for back-compat; new code should import from here.
"""

from __future__ import annotations

import os

# ── Provider model defaults ──────────────────────────────────────────

VERTEX_LLM_DEFAULT_MODEL = "gemini-2.0-flash-lite"
GEMINI_DEFAULT_MODEL = os.environ.get(
    "GEMINI_DEFAULT_MODEL", "gemini-3.1-flash-lite-preview"
)

# OpenAI's chat-completions base URL — used by ``OpenAILLMProvider``
# (and for openrouter / anthropic compat where the API mirrors OpenAI's
# shape, with the base URL swapped via tenant-config override).
OPENAI_CHAT_BASE_URL = "https://api.openai.com/v1"

# Anthropic + OpenRouter base URLs and default models. The
# ``OpenAILLMProvider`` works against any of these endpoints by varying
# ``base_url``; the registry picks the right tuple based on
# ``ProviderName``.
ANTHROPIC_CHAT_BASE_URL = "https://api.anthropic.com/v1"
ANTHROPIC_DEFAULT_MODEL = os.environ.get(
    "ANTHROPIC_DEFAULT_MODEL", "claude-haiku-4-5-20251001"
)  # Anthropic API requires native model IDs
OPENROUTER_CHAT_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_DEFAULT_MODEL = os.environ.get(
    "OPENROUTER_DEFAULT_MODEL", "openai/gpt-5.4-nano"
)

# ── Retry policy ─────────────────────────────────────────────────────

# Retries on primary provider before falling back to a secondary
# provider (configured via ``call_with_fallback``). Linear backoff
# rather than exponential because the LLM call is already slow (1-3s)
# and a multi-second backoff would push the request past timeout.
LLM_RETRY_ATTEMPTS = 2
LLM_RETRY_DELAY_S = 1.0

# Fallback model for OpenAI-compatible providers when the tenant's
# configured model is not set — env-overridable so on-call can swap to
# a cheaper / different family without a redeploy.
LLM_FALLBACK_MODEL_OPENAI = os.environ.get("LLM_FALLBACK_MODEL_OPENAI", "gpt-5.4-nano")


# Per-call timeout passed to the OpenAI/Anthropic/Openrouter SDK.
# Without an explicit value the SDK rides httpx's default — long
# enough that a single hung upstream call eats the whole enrichment
# budget silently. 25s gives the provider room to respond while still
# leaving budget for one retry under the inline ceiling.
def _read_float_env(name: str, default: float | None) -> float | None:
    """Parse a float env var defensively.

    Bare ``float(os.environ.get(...))`` would raise ``ValueError`` at
    module import on a misconfigured value (e.g. ``"25s"`` instead of
    ``"25"``), crashing the entire worker / core-api startup before any
    structured-logging is wired. Catch it here, write a warning to
    stderr, and fall back to the documented default.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        # Module-level import has no logger configured yet — print to
        # stderr with the key name so the bad value is visible in the
        # crash log even before structlog wires up.
        import sys

        print(
            f"WARN: {name}={raw!r} is not a valid float; falling back to {default}",
            file=sys.stderr,
        )
        return default


OPENAI_REQUEST_TIMEOUT_SECONDS = _read_float_env("OPENAI_REQUEST_TIMEOUT_SECONDS", 25.0)


# Hard ceiling on the *whole* business/personal pre-gate classification —
# across retries and any provider fallback — enforced by the classifier itself
# (``classify_business_personal`` wraps the call in ``asyncio.wait_for``). The
# pre-gate is a fast, fail-open go/no-go that runs inline on the write path
# BEFORE the row is written, so a slow or unreachable provider must never stall
# a write: exceeding this ceiling fails open (the post-enrichment gate remains
# the backstop). Deliberately aggressive — much tighter than the per-call SDK
# read timeout above — because the cost of a too-low value is only a missed
# early-reject, never a blocked write. Env-tunable per deployment.
PREGATE_CLASSIFIER_TIMEOUT_SECONDS = _read_float_env(
    "PREGATE_CLASSIFIER_TIMEOUT_SECONDS", 5.0
)


# ── httpx pool sizing for OpenAI-compatible providers ────────────────
#
# CAURA-627: the SDK rides httpx's default (100 max_connections / 20
# keepalive). Under bulk-write storms — 100-item batches with
# ``BULK_ENRICHMENT_CONCURRENCY=10`` × ``per_tenant_write_concurrency=16``
# in flight per worker — the pool saturates and queues subsequent
# requests, including other tenants'. The defaults were sized for
# request/response patterns where one user's bursty fan-out doesn't
# sit on top of another tenant's traffic; our hot-path enrichment is
# exactly that pattern.
#
# Sized for headroom over the worst-case fan-out per process. Env-
# tunable so an operator can adjust during an incident (e.g. if the
# upstream provider's per-IP cap is hit) without a redeploy.
from common.env_utils import clamp_keepalive, read_int_env  # noqa: E402

OPENAI_HTTPX_MAX_CONNECTIONS = read_int_env("OPENAI_HTTPX_MAX_CONNECTIONS", 200)
OPENAI_HTTPX_MAX_KEEPALIVE_CONNECTIONS = clamp_keepalive(
    OPENAI_HTTPX_MAX_CONNECTIONS,
    read_int_env("OPENAI_HTTPX_MAX_KEEPALIVE_CONNECTIONS", 50),
)


# ── httpx per-phase timeouts for OpenAI-compatible providers ─────────
#
# Passing a bare float to ``AsyncOpenAI(timeout=...)`` keeps httpx's
# default 5 s connect/pool phases. On Cloud Run with a VPC connector in
# ``all-traffic`` egress mode, EVERY outbound call (including public
# LLM APIs) rides the connector + Cloud NAT, and a cold connection —
# first call after idle, keepalive pool drained, NAT state churn —
# intermittently exceeds 5 s. Observed in prod as a steady trickle of
# ``httpcore.ConnectTimeout`` from the enrichment / entity-extraction
# handlers ("pubsub handler raised; nacking for redelivery" +
# "Entity extraction failed" — the latter permanently skips entity
# links for that memory). The read phase stays governed by
# ``OPENAI_REQUEST_TIMEOUT_SECONDS``; only connect/pool get headroom.
OPENAI_HTTPX_CONNECT_TIMEOUT_SECONDS = _read_float_env(
    "OPENAI_HTTPX_CONNECT_TIMEOUT_SECONDS", 15.0
)
# None ⇒ the pool phase tracks the per-instance request budget
# (``request_timeout_seconds``), keeping exact parity with the
# bare-float behaviour this replaced for EVERY configuration — a
# deployment running OPENAI_REQUEST_TIMEOUT_SECONDS=60 previously got a
# 60 s pool wait and still does. Set the env var only to decouple them
# (e.g. fail-fast under pool pressure).
OPENAI_HTTPX_POOL_TIMEOUT_SECONDS: float | None = _read_float_env(
    "OPENAI_HTTPX_POOL_TIMEOUT_SECONDS", None
)
