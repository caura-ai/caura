"""Back-compat re-export shim for :mod:`common.llm._platform` (CAURA-595).

The platform LLM singleton implementation moved to
``common/llm/_platform.py`` so core-worker can construct the same
singleton from the same ``PLATFORM_LLM_*`` env vars without depending on
core-api's settings module.

This shim keeps ``from core_api.providers._platform import …`` working
unchanged.
"""

from __future__ import annotations

from common.llm._platform import (
    get_platform_embedding,
    get_platform_init_errors,
    get_platform_llm,
    init_platform_providers,
)

__all__ = [
    "get_platform_embedding",
    "get_platform_init_errors",
    "get_platform_llm",
    "init_platform_providers",
]
