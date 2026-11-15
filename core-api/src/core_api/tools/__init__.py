"""Tool-spec single source of truth for the MCP surface.

Importing this package autoloads every `caura_*.py` spec module and
populates `REGISTRY`. Use `get_spec(name)` for typed lookup.
"""

from __future__ import annotations

from ._builders import extract_param_descriptors, mcp_register, to_descriptor_json, tool_annotations
from ._registry import REGISTRY, all_specs, get_spec, register, specs_for_surface
from ._types import (
    ALL_SURFACES,
    DEFAULT_SURFACE,
    DIRECTORY_SURFACE,
    HandlerFn,
    ImplStatus,
    OpSpec,
    Surface,
    ToolSpec,
)

__all__ = [
    "ALL_SURFACES",
    "DEFAULT_SURFACE",
    "DIRECTORY_SURFACE",
    "REGISTRY",
    "HandlerFn",
    "ImplStatus",
    "OpSpec",
    "Surface",
    "ToolSpec",
    "all_specs",
    "extract_param_descriptors",
    "get_spec",
    "mcp_register",
    "register",
    "specs_for_surface",
    "to_descriptor_json",
    "tool_annotations",
]
