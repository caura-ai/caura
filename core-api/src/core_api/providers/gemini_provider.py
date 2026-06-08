"""Back-compat re-export shim (CAURA-595).

Implementation moved to :mod:`common.llm.providers.gemini`.
"""

from __future__ import annotations

from common.llm.providers.gemini import GeminiLLMProvider

__all__ = ["GeminiLLMProvider"]
