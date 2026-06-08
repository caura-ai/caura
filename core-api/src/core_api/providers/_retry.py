"""Back-compat re-export shim for :mod:`common.llm.retry` (CAURA-595).

The implementation moved to ``common/llm/retry.py`` so core-worker can
share it without importing core-api. This shim keeps existing call
sites (``core_api.services.*``, tests) working unchanged.
"""

from __future__ import annotations

from common.llm.retry import call_with_fallback, call_with_retry

__all__ = ["call_with_fallback", "call_with_retry"]
