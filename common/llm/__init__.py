"""LLM adapter shared across services.

Public surface:

* :class:`~common.llm.protocols.LLMProvider` — async protocol every
  provider implements.
* :func:`call_with_retry` / :func:`call_with_fallback` — async wrappers
  that handle linear-backoff retries and the 3-tier fallback chain
  (primary → tenant fallback → fake).
* :func:`get_llm_provider` — factory; resolves credentials in three
  tiers (tenant key → platform singleton → fake).
* :func:`init_platform_providers` / :func:`get_platform_llm` —
  platform-tier singleton initialised once at startup from
  ``PLATFORM_LLM_*`` env vars.
* :class:`~common.llm.providers.fake.FakeLLMProvider` — deterministic,
  zero-dependency provider for tests/dev and last-resort fallback.

Reads provider settings from environment variables (``OPENAI_API_KEY``,
``ANTHROPIC_API_KEY``, ``OPENROUTER_API_KEY``, ``GEMINI_API_KEY``,
``PLATFORM_LLM_*``) — no dependency on any service-specific config
module — so both core-api (tenant-aware) and core-worker
(platform-only) share the same implementation.

CAURA-595 full form: extracted from ``core_api.protocols``,
``core_api.providers.*`` and ``core_api.services.memory_enrichment`` so
that ``core-worker`` can handle enrich-request events without
depending on ``core-api``.
"""

from __future__ import annotations

from common.llm._credentials import (
    has_credentials,
    resolve_gemini_config,
    resolve_openai_compatible,
)
from common.llm._platform import (
    get_platform_embedding,
    get_platform_init_errors,
    get_platform_llm,
    init_platform_providers,
)
from common.llm.protocols import LLMProvider
from common.llm.providers.fake import FakeLLMProvider
from common.llm.registry import get_llm_provider
from common.llm.retry import call_with_fallback, call_with_retry

__all__ = [
    "FakeLLMProvider",
    "LLMProvider",
    "call_with_fallback",
    "call_with_retry",
    "get_llm_provider",
    "get_platform_embedding",
    "get_platform_init_errors",
    "get_platform_llm",
    "has_credentials",
    "init_platform_providers",
    "resolve_gemini_config",
    "resolve_openai_compatible",
]
