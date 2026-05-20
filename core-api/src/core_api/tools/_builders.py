"""Helpers that derive other surfaces from a `ToolSpec`.

- `mcp_register(mcp, spec)` — register the spec's handler with FastMCP.
- `to_descriptor_json(spec)` — full-spec JSON for `/tool-descriptions` (new shape).
- `extract_param_descriptors(handler)` — introspect handler signature → param list.

Param info is intentionally derived from the handler's `Annotated[type, Field(...)]`
signature rather than restated on `ToolSpec` — keeps the handler authoritative.
"""

from __future__ import annotations

import inspect
import typing
from typing import Annotated, Any, get_args, get_origin

from pydantic.fields import FieldInfo

from ._types import ToolSpec


def mcp_register(mcp, spec: ToolSpec) -> None:
    """Register `spec.handler` with the FastMCP instance, using `spec.description`.

    No-op when `spec.handler is None` (Phase 1 of the refactor — handlers
    still live in `mcp_server.py`'s `@mcp.tool` decorators).

    Handlers are annotated `-> str` but their error paths return
    `CallToolResult` via `_as_error_result` to preserve the unified
    error-envelope clients parse from `content[0].text`. With structured
    output enabled, FastMCP would auto-generate a `{result: str}` schema
    from the `-> str` annotation and reject the `CallToolResult` at
    validation time, so disable it here.
    """
    if spec.handler is None:
        return
    mcp.tool(
        name=spec.name,
        description=spec.description,
        structured_output=False,  # mcp>=1.10 (PR #993); current pin >=1.26 covers it
    )(spec.handler)


def _python_type_name(annotation: Any) -> str:
    """Render a Python type annotation as a short string for descriptors.

    Normalizes a few Python-specific shapes to friendlier forms:
    - `NoneType` → `None`
    - `UnionType[A, B, None]` → `A | B | None` (PEP 604 pipe syntax)
    """
    import types

    if annotation is inspect.Parameter.empty:
        return "any"
    if annotation is type(None):
        return "None"
    origin = get_origin(annotation)
    if origin is None:
        if hasattr(annotation, "__name__"):
            return annotation.__name__
        return repr(annotation)
    args = get_args(annotation)
    # Strip away the Annotated[T, ...] wrapper if it sneaks in.
    if origin is Annotated:
        return _python_type_name(args[0])
    # PEP 604 `A | B` unions render as `typing.Union` origin; surface with `|`.
    if origin in (types.UnionType,) or str(origin) == "typing.Union":
        return " | ".join(_python_type_name(a) for a in args)
    arg_names = ", ".join(_python_type_name(a) for a in args)
    if hasattr(origin, "__name__"):
        return f"{origin.__name__}[{arg_names}]"
    return f"{origin}[{arg_names}]"


def _unwrap_annotated(annotation: Any) -> tuple[Any, FieldInfo | None]:
    """Pull `(real_type, FieldInfo|None)` out of an `Annotated[T, Field(...)]`."""
    if get_origin(annotation) is Annotated:
        args = get_args(annotation)
        real_type = args[0]
        field_info: FieldInfo | None = None
        for meta in args[1:]:
            if isinstance(meta, FieldInfo):
                field_info = meta
                break
        return real_type, field_info
    return annotation, None


def extract_param_descriptors(handler) -> list[dict[str, Any]]:
    """Introspect `handler` signature → list of `{name, type, description, default, required}`.

    Returns an empty list when `handler is None` (Phase 1 stub).
    """
    if handler is None:
        return []
    sig = inspect.signature(handler)
    out: list[dict[str, Any]] = []
    type_hints = typing.get_type_hints(handler, include_extras=True)
    for p_name, p in sig.parameters.items():
        if p_name in ("self", "cls"):
            continue
        annotation = type_hints.get(p_name, p.annotation)
        real_type, field_info = _unwrap_annotated(annotation)
        descriptor: dict[str, Any] = {
            "name": p_name,
            "type": _python_type_name(real_type),
            "required": p.default is inspect.Parameter.empty,
        }
        if p.default is not inspect.Parameter.empty:
            # Skip non-JSON-serializable defaults safely.
            try:
                import json as _json

                _json.dumps(p.default)
                descriptor["default"] = p.default
            except (TypeError, ValueError):
                descriptor["default"] = repr(p.default)
        if field_info is not None and field_info.description:
            descriptor["description"] = field_info.description
        out.append(descriptor)
    return out


def to_descriptor_json(spec: ToolSpec) -> dict[str, Any]:
    """Full descriptor for `/tool-descriptions` (new SoT shape)."""
    return {
        "name": spec.name,
        "description": spec.description,
        "trust_required": spec.trust_required,
        "impl_status": spec.impl_status,
        "plugin_exposed": spec.plugin_exposed,
        "params": extract_param_descriptors(spec.handler),
        "ops": [
            {
                "name": op.name,
                "description": op.description,
                "required_params": list(op.required_params),
                "trust_required": op.trust_required or spec.trust_required,
            }
            for op in spec.ops
        ],
        "error_codes": list(spec.error_codes),
    }
