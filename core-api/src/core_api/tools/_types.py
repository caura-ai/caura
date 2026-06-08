"""Core dataclasses for the tool-spec single source of truth.

A `ToolSpec` is the declarative description of a single MCP tool. The
`mcp_server` derives `@mcp.tool` registrations from these. The plugin reads
the same shape via `tools.json` (CI-synced from `scripts/export_tool_specs.py`).

Parameter info is intentionally NOT duplicated here — it lives on the handler
function's `Annotated[type, Field(description=...)]` signature and is
introspected on demand. That keeps the spec lean and the handler the single
source of truth for its own arguments.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from mcp.types import CallToolResult

HandlerFn = Callable[..., Awaitable[str | CallToolResult]]
"""Async; success returns a string, errors return ``CallToolResult`` via
``_as_error_result``. Type checkers accept ``CallToolResult`` on any path —
returning it from a success branch silently sets ``isError=True`` for
clients; enforce the split in code review. Registration sets
``structured_output=False`` to accept this union — see ``mcp_register``
in ``_builders.py``."""


ImplStatus = Literal["live", "reserved", "deprecated"]
"""
- live       — handler does the real thing
- reserved   — handler returns NOT_IMPLEMENTED; slot reserved for a teammate
- deprecated — kept for one release; will be removed
"""


@dataclass(frozen=True)
class OpSpec:
    """One op of an op-dispatched tool (e.g., memclaw_doc op=write|read|query|delete)."""

    name: str
    description: str
    required_params: tuple[str, ...] = ()
    trust_required: int = 0  # 0 = inherit from ToolSpec.trust_required


@dataclass(frozen=True)
class ToolSpec:
    """Declarative spec for a single MCP tool. Drives MCP registration,
    `/tool-descriptions` output, plugin `tools.json`, and per-tool trust gating.

    Param schemas are derived from `handler`'s signature at runtime — do NOT
    restate them here.
    """

    name: str
    description: str
    handler: HandlerFn | None = None
    """Handler is None during Phase 1 of the v1.0 refactor (handlers still
    live in `mcp_server.py`'s `@mcp.tool` decorators). Phase 2 wires real
    handlers in and `mcp_register` becomes the registration path."""
    trust_required: int = 0
    """Baseline trust to invoke. 0 = no MCP-level gate (existing behavior for
    tools that enforce trust deeper in the service layer)."""

    impl_status: ImplStatus = "live"
    plugin_exposed: bool = True
    """Whether the OpenClaw plugin should expose this tool. STM tools and
    placeholders set False."""

    ops: tuple[OpSpec, ...] = field(default_factory=tuple)
    """Optional — for op-dispatched tools. Empty for non-dispatch tools."""

    error_codes: tuple[str, ...] = field(default_factory=tuple)
    """Codes this tool may emit in the unified error envelope."""

    def __post_init__(self) -> None:
        # Light invariants (full validation happens in _registry on import).
        if not self.name.startswith("memclaw_"):
            raise ValueError(f"Tool name must start with 'memclaw_': {self.name}")
        if self.trust_required not in (0, 1, 2, 3):
            raise ValueError(f"trust_required must be 0..3, got {self.trust_required} for {self.name}")
        if self.impl_status == "reserved" and self.plugin_exposed:
            raise ValueError(f"reserved tools must set plugin_exposed=False: {self.name}")
