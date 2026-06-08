"""LLM provider implementations.

Public exports — callers tagging metrics or routing alerts on the
shape-error class should import from this package rather than the
underscore-prefixed implementation module so the private file layout
can shift without breaking them.
"""

from common.llm.providers._shape_error import ProviderResponseShapeError

__all__ = ["ProviderResponseShapeError"]
