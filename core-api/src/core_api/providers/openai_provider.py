"""Back-compat re-export shim (CAURA-595).

Implementation moved to :mod:`common.llm.providers.openai`.
"""

from __future__ import annotations

from common.llm.providers.openai import OpenAILLMProvider

__all__ = ["OpenAILLMProvider"]
