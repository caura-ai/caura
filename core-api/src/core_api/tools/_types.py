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


Surface = Literal["default", "directory"]
"""Which tool set a connection is served.

- default   — every API-key agent, the OpenClaw plugin, and any request whose
              surface was not set by the gateway. The set it has always been.
- directory — a Claude connector-directory connection (MCP OAuth through the
              gateway, which marks it ``X-Caura-Tool-Surface: directory``). The
              directory requires that no tool mixes reads and writes, so this
              set swaps the two op-dispatched tools that do (``caura_manage``,
              ``caura_doc``) for narrower tools reaching the same handlers.
"""

DEFAULT_SURFACE: Surface = "default"
DIRECTORY_SURFACE: Surface = "directory"
ALL_SURFACES: tuple[Surface, ...] = (DEFAULT_SURFACE, DIRECTORY_SURFACE)


ImplStatus = Literal["live", "reserved", "deprecated"]
"""
- live       — handler does the real thing
- reserved   — handler returns NOT_IMPLEMENTED; slot reserved for a teammate
- deprecated — kept for one release; will be removed
"""


@dataclass(frozen=True)
class OpSpec:
    """One op of an op-dispatched tool (e.g., caura_doc op=write|read|query|delete)."""

    name: str
    description: str
    required_params: tuple[str, ...] = ()
    trust_required: int = 0  # 0 = inherit from ToolSpec.trust_required


@dataclass(frozen=True)
class ToolSpec:
    """Declarative spec for a single MCP tool. Drives MCP registration,
    `/tool-descriptions` output and plugin `tools.json`.

    ``trust_required`` is published metadata, not the gate: the MCP handlers
    take their thresholds from scope, and ``test_mcp_authz_gate_inventory``
    holds every published level to a gate that can refuse (L-215).

    Param schemas are derived from `handler`'s signature at runtime — do NOT
    restate them here.
    """

    name: str
    description: str
    handler: HandlerFn | None = None
    """The tool's implementation, in `mcp_server.py`.

    Every registered spec sets one, and `test_tools_registry` asserts so, which
    is why `mcp_register`'s `handler is None` guard is unreachable in practice.
    The `None` remains only because it is the dataclass default; removing it and
    the guard is a separate change.

    An earlier version of this note described a "Phase 1" in which handlers were
    still `None` and lived on `@mcp.tool` decorators, with `mcp_register` yet to
    become the registration path. All three were true once and none is now:
    `mcp_server.py` carries no `@mcp.` decorator at all, and each `caura_*` spec
    module calls `mcp_register` itself at import."""
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

    title: str = ""
    """Human-readable name, published as the MCP tool ``title`` and as
    ``annotations.title``. Required: ``__post_init__`` refuses an empty one."""

    read_only: bool = False
    """Published as ``readOnlyHint``. True only when no op of the tool changes
    stored data. Bookkeeping a read leaves behind (usage meters, recall
    counters, audit and read-receipt rows) does not count as a write here; a
    row the caller can see change does. A tool with even one writing op is not
    read-only, which is why the op-dispatched ``caura_manage`` and ``caura_doc``
    are not."""

    destructive: bool = True
    """Published as ``destructiveHint``, and ignored (published False) when
    ``read_only`` is set. True when the tool can change or remove data that
    already exists rather than only adding to it. MCP's own default for a
    non-read-only tool is true, so it is the default here too, and setting it
    False needs a reason. ``destructive_hint`` is the resolved value."""

    @property
    def destructive_hint(self) -> bool:
        return self.destructive and not self.read_only

    surfaces: tuple[Surface, ...] = ALL_SURFACES
    """The surfaces whose ``tools/list`` names this tool. A tool is callable
    exactly where it is listed: ``call_tool`` refuses it elsewhere with
    ``TOOL_NOT_ON_SURFACE``."""

    directory_description: str | None = None
    """The description served on the directory surface, when the default one
    says something the Claude connector directory does not accept (telling the
    model to set aside user instructions, to call a tool unprompted, or naming
    a tool that surface does not list). ``None`` serves ``description`` there
    too. The default surface, ``/tool-descriptions`` and the plugin manifest
    always carry ``description``."""

    delegate: str | None = None
    """For a tool that narrows another one: the tool whose handler it calls.
    Its ops must be a subset of the delegate's, and its handler forwards to the
    delegate's handler and does nothing else (``test_tool_surfaces`` checks
    both), so it inherits every gate the delegate applies."""

    def __post_init__(self) -> None:
        # Light invariants (full validation happens in _registry on import).
        if not self.name.startswith("caura_"):
            raise ValueError(f"Tool name must start with 'caura_': {self.name}")
        if self.trust_required not in (0, 1, 2, 3):
            raise ValueError(f"trust_required must be 0..3, got {self.trust_required} for {self.name}")
        if self.impl_status == "reserved" and self.plugin_exposed:
            raise ValueError(f"reserved tools must set plugin_exposed=False: {self.name}")
        if not self.title.strip():
            raise ValueError(f"Tool must declare a title: {self.name}")
        if not self.surfaces or not set(self.surfaces) <= set(ALL_SURFACES):
            raise ValueError(f"surfaces must be a non-empty subset of {ALL_SURFACES}: {self.name}")
        if self.plugin_exposed and DEFAULT_SURFACE not in self.surfaces:
            # The plugin is a default-surface client; exposing a tool there
            # that the MCP default surface hides would split the two again.
            raise ValueError(f"plugin_exposed tools must be on the default surface: {self.name}")
