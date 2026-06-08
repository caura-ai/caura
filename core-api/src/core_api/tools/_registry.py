"""Single-source-of-truth registry of all MCP tools.

Spec modules each declare a `SPEC: ToolSpec` and are imported here. The
import side effect appends to `REGISTRY`. Validation invariants run at
module import time so any drift surfaces immediately.

Lookup by name: `REGISTRY[name]` or `get_spec(name)`.
"""

from __future__ import annotations

import importlib
import pkgutil

from ._types import ToolSpec

REGISTRY: dict[str, ToolSpec] = {}
"""All registered tool specs, keyed by tool name. Populated at import time."""


def register(spec: ToolSpec) -> None:
    """Add a spec to the registry. Spec modules call this from module top-level."""
    if spec.name in REGISTRY:
        raise ValueError(
            f"Duplicate tool spec for {spec.name!r} (existing: {REGISTRY[spec.name]!r}, new: {spec!r})"
        )
    REGISTRY[spec.name] = spec


def get_spec(name: str) -> ToolSpec:
    """Lookup helper with a clear error message for missing tools."""
    try:
        return REGISTRY[name]
    except KeyError:
        raise KeyError(f"Unknown tool {name!r}. Known: {sorted(REGISTRY.keys())}") from None


def all_specs() -> list[ToolSpec]:
    """Snapshot of all registered specs in registration order."""
    return list(REGISTRY.values())


def required_trust(tool_name: str, op: str | None = None) -> int:
    """Required trust level for a given tool (and optional op).

    Falls back to 0 (no MCP-level gate) for unknown tools so this is safe
    to call from existing `agent_service.enforce_*` shims as a soft lookup.
    """
    spec = REGISTRY.get(tool_name)
    if spec is None:
        return 0
    if op is not None:
        for op_spec in spec.ops:
            if op_spec.name == op and op_spec.trust_required > 0:
                return op_spec.trust_required
    return spec.trust_required


def _autoload_specs() -> None:
    """Import every `memclaw_*` module in this package; each registers its spec."""
    package_name = __name__.rsplit(".", 1)[0]  # "core_api.tools"
    package = importlib.import_module(package_name)
    for mod_info in pkgutil.iter_modules(package.__path__):
        name = mod_info.name
        if not name.startswith("memclaw_"):
            continue  # skip _types, _builders, etc.
        importlib.import_module(f"{package_name}.{name}")


def _validate() -> None:
    """Whole-registry invariants checked after autoload completes."""
    for name, spec in REGISTRY.items():
        for op_spec in spec.ops:
            # Op trust thresholds shouldn't be lower than tool baseline.
            if op_spec.trust_required and op_spec.trust_required < spec.trust_required:
                raise ValueError(
                    f"{name}: op={op_spec.name!r} trust_required={op_spec.trust_required} "
                    f"is below tool baseline {spec.trust_required}"
                )


_autoload_specs()
_validate()
