"""Back-compat re-export shim (CAURA-595).

Implementation moved to :mod:`common.llm.providers.fake`.
"""

from __future__ import annotations

from common.llm.providers.fake import FakeLLMProvider

__all__ = ["FakeLLMProvider"]
