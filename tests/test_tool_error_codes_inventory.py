"""``ToolSpec.error_codes`` must be the codes the handler can actually emit.

The field is published: ``scripts/export_tool_specs.py`` writes it into
``plugin/tools.json``, ``test_tools_export_in_sync`` holds the two in lockstep,
and ``plugin/src/tool-specs.ts`` types it as part of the manifest clients read.
Nothing in ``plugin/src`` reads it at runtime — like ``ops[]`` before #1373, it
is a claim rather than a mechanism — which is exactly why it needs a test: a
claim with no consumer has nothing to contradict it when it goes stale.

Measured before this existed: 7 of 12 specs declared NOTHING, 52 emittable
codes went undeclared, and no spec declared a code it could not emit. So the
field was never WRONG, only silent — the shape of defect that reads as a
considered "this tool has no error codes" and is impossible to distinguish
from one.

WHY THE SAME FOUR CODES RECUR. Every handler opens with ``_check_auth``, which
can return ``UNAUTHORIZED`` or ``FORBIDDEN``, and most then call
``_refuse_default_agent_on_gateway`` (``MISSING_AGENT_ID``) and validate
arguments (``INVALID_ARGUMENTS``). Those four account for 34 of the 52
additions. They are declared per tool rather than hoisted into an
"envelope codes" constant on purpose: ``FORBIDDEN`` is *also* raised directly
by five handlers, so a hoisted set would make the invariant
``emitted == declared | ENVELOPE`` — which cannot see a spurious envelope code
in a spec, and blind spots in this scan are the one thing this file exists to
avoid.
"""

from __future__ import annotations

import pytest

import core_api.tools as tools
from tests._error_codes import emitted_codes, prebaked_error_constants

pytestmark = pytest.mark.unit


# Codes a spec may declare without the scan finding them. Empty, and a new
# entry has to be argued for: every declared code is currently corroborated.
DECLARED_NOT_EMITTED_ALLOWLIST: dict[str, str] = {}


def _specs():
    return sorted(tools.REGISTRY.values(), key=lambda s: s.name)


def test_every_emittable_code_is_declared() -> None:
    """A code a client can receive but the manifest never mentions."""
    offenders = []
    for spec in _specs():
        if spec.handler is None:
            continue
        missing = sorted(emitted_codes(spec.handler.__name__) - set(spec.error_codes))
        if missing:
            offenders.append(f"    {spec.name}: {missing}")
    assert not offenders, (
        f"{len(offenders)} tool(s) can emit a code they do not declare:\n"
        + "\n".join(offenders)
        + "\n\nAdd the codes to the spec's error_codes and regenerate "
        "plugin/tools.json via scripts/export_tool_specs.py."
    )


def test_every_declared_code_is_reachable() -> None:
    """The other direction: a code promised but unreachable is also a false
    claim, and it is how a copy-pasted spec starts lying."""
    offenders = []
    for spec in _specs():
        if spec.handler is None or spec.name in DECLARED_NOT_EMITTED_ALLOWLIST:
            continue
        phantom = sorted(set(spec.error_codes) - emitted_codes(spec.handler.__name__))
        if phantom:
            offenders.append(f"    {spec.name}: {phantom}")
    assert not offenders, (
        f"{len(offenders)} tool(s) declare a code no reachable branch emits:\n"
        + "\n".join(offenders)
        + "\n\nRemove it, or add a line to DECLARED_NOT_EMITTED_ALLOWLIST in "
        f"{__file__} explaining what emits it."
    )


# --- guards on the scan itself ---------------------------------------------
#
# Every assertion above compares against `emitted_codes`. A scan that returns
# LESS makes them pass, so the scan needs its own floor.


def test_the_scan_finds_the_prebaked_constants() -> None:
    """The path a function-scoped walk misses.

    ``_check_auth`` returns module-level ``CallToolResult`` constants rather
    than formatting anything, so the codes reached through it are invisible
    unless top-level assignments are read. The first version of this scan
    missed 19 codes that way and reported ``UNAUTHORIZED`` as emitted by no
    tool at all. Pinned by code, not by constant name, so renaming
    ``_AUTH_ERROR`` does not fail this while removing the mechanism does.
    """
    found = prebaked_error_constants()
    codes = {code for codes in found.values() for code in codes}
    assert "UNAUTHORIZED" in codes, (
        "no pre-baked constant yields UNAUTHORIZED — either _check_auth stopped "
        f"using one, or the scan stopped reading top-level assignments. Found: {found}"
    )
    assert "FORBIDDEN" in codes, (
        f"no pre-baked constant yields FORBIDDEN (read-only scope gate). Found: {found}"
    )


def test_the_scan_follows_helpers_and_dict_literals() -> None:
    """The other two paths, pinned on the tools that exercise each.

    ``caura_manage`` builds its unknown-op envelope as a dict literal instead
    of calling ``_error_response``, and reaches ``MISSING_AGENT_ID`` only
    through ``_refuse_default_agent_on_gateway``. If either path stops being
    followed, the invariants above go quiet rather than failing.
    """
    manage = emitted_codes("caura_manage")
    assert "INVALID_ARGUMENTS" in manage, (
        "the dict-literal path is not being read — caura_manage's unknown-op "
        "envelope is written as {'code': 'INVALID_ARGUMENTS'}"
    )
    assert "MISSING_AGENT_ID" in manage, (
        "helper calls are not being followed — caura_manage reaches this code "
        "only via _refuse_default_agent_on_gateway"
    )
    assert "UNAUTHORIZED" in manage, (
        "the auth preamble is not being followed — every handler calls _check_auth"
    )


def test_the_scan_is_not_silently_empty() -> None:
    """Guards the guard: a scan returning nothing would pass everything."""
    per_tool = {
        spec.name: emitted_codes(spec.handler.__name__)
        for spec in _specs()
        if spec.handler is not None
    }
    assert len(per_tool) >= 10, f"only {len(per_tool)} handlers scanned"
    empty = sorted(name for name, codes in per_tool.items() if not codes)
    assert not empty, (
        f"the scan found no emittable code at all for {empty} — every MCP "
        "handler opens with _check_auth, so an empty result means the scan "
        "broke, not that the tool is infallible."
    )
