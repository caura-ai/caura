"""Back-compat re-export shim (CAURA-595).

Implementation moved to :mod:`common.llm.providers.vertex`.
"""

from __future__ import annotations

from common.llm.providers.vertex import VertexLLMProvider

__all__ = ["VertexLLMProvider"]
