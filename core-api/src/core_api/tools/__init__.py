"""Tool-spec single source of truth for the MCP surface.

Importing this package autoloads every `memclaw_*.py` spec module and
populates `REGISTRY`. Use `get_spec(name)` for typed lookup.
"""

from __future__ import annotations

from ._builders import extract_param_descriptors, mcp_register, to_descriptor_json
from ._registry import REGISTRY, all_specs, get_spec, register, required_trust
from ._types import HandlerFn, ImplStatus, OpSpec, ToolSpec

__all__ = [
    "REGISTRY",
    "HandlerFn",
    "ImplStatus",
    "OpSpec",
    "ToolSpec",
    "all_specs",
    "extract_param_descriptors",
    "get_spec",
    "mcp_register",
    "register",
    "required_trust",
    "to_descriptor_json",
]
