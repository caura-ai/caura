"""Permanent-alias contract for the 2026-08-14 memclaw_* → caura_* rename.

Three guarantees, each load-bearing forever:

1. ``tools/list`` advertises ONLY ``caura_*`` names — the old names must
   never reappear in the listing (dual-listing doubles the client's
   tool-schema token budget, which is why the alias lives at dispatch).
2. Every listed tool is callable under its legacy ``memclaw_*`` name —
   the ``_InstrumentedFastMCP.call_tool`` shim translates before
   dispatch, so saved prompts, keystone rules, and published tutorials
   written against the old names keep working.
3. The set of alias-covered tools is derived from the LIVE registry, not
   a hardcoded list — a tool added tomorrow is covered by these tests
   automatically, and a change that breaks the shim fails loudly here.

The calls go through ``mcp.call_tool`` — the same dispatch point JSON-RPC
``tools/call`` uses. Because handlers check auth in their bodies (after
FastMCP's argument validation), per-tool coverage asserts *equivalence*:
whatever the canonical name produces for a given call (an envelope, a
validation ToolError), the legacy name must produce the same — no
per-tool argument fixtures needed, and never FastMCP's "Unknown tool".
"""

from __future__ import annotations

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from core_api import mcp_server
from tests._mcp_test_helpers import as_text, parse_envelope

pytestmark = pytest.mark.unit


async def _listed_tool_names() -> list[str]:
    return [tool.name for tool in await mcp_server.mcp.list_tools()]


async def _outcome(name: str) -> tuple[str, str]:
    """Call ``name`` with empty args; return a comparable (kind, detail)."""
    # Normalize BOTH spellings out of the message so canonical/legacy
    # outcomes compare equal. The shim translates before dispatch, so
    # error text mentions the canonical name under either spelling; what
    # matters is the error class (validation vs unknown-tool), not which
    # spelling was used.
    suffix = name.removeprefix("caura_").removeprefix("memclaw_")

    def _normalize(text: str) -> str:
        return text.replace(f"caura_{suffix}", "<tool>").replace(
            f"memclaw_{suffix}", "<tool>"
        )

    try:
        result = await mcp_server.mcp.call_tool(name, {})
    except ToolError as exc:
        return ("tool_error", _normalize(str(exc)))
    return ("result", _normalize(as_text(result)))


@pytest.mark.asyncio
async def test_listing_exposes_only_caura_names():
    names = await _listed_tool_names()
    assert names, "registry came up empty — autoloader glob broken?"
    offenders = [n for n in names if not n.startswith("caura_")]
    assert not offenders, (
        f"tools/list must advertise only caura_* names, got {offenders}. "
        "Legacy memclaw_* names are dispatch aliases, never listed — "
        "dual-listing doubles every client's tool-schema token budget."
    )


@pytest.mark.asyncio
async def test_every_tool_dispatches_under_its_legacy_name():
    """memclaw_<suffix> must behave exactly like caura_<suffix>, for every
    registered tool — including tools that did not exist at rename time.
    """
    for name in await _listed_tool_names():
        legacy = "memclaw_" + name.removeprefix("caura_")
        canonical_kind, canonical_detail = await _outcome(name)
        legacy_kind, legacy_detail = await _outcome(legacy)
        assert "unknown tool" not in legacy_detail.lower(), (
            f"legacy alias {legacy!r} did not reach the {name!r} handler — "
            "the permanent rename shim in _InstrumentedFastMCP.call_tool "
            "is broken, and every pre-rename saved prompt breaks with it"
        )
        assert (legacy_kind, legacy_detail) == (canonical_kind, canonical_detail), (
            f"{legacy!r} and {name!r} diverged: the alias must be "
            "indistinguishable from the canonical name"
        )


@pytest.mark.asyncio
async def test_legacy_alias_reaches_the_handler_body(monkeypatch):
    """Beyond name resolution: prove a legacy-named call executes the
    handler. With auth stubbed to the pre-baked UNAUTHORIZED envelope and
    valid arguments, only the real handler body can produce this error.
    """
    monkeypatch.setattr(mcp_server, "_check_auth", lambda: mcp_server._AUTH_ERROR)

    result = await mcp_server.mcp.call_tool(
        "memclaw_write",
        {"content": "alias probe body", "agent_id": "claude-eldad"},
    )
    envelope = parse_envelope(result)
    assert envelope["error"]["code"] == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_unknown_names_still_fail_under_both_prefixes():
    """The shim must not turn nonexistent tools into false positives."""
    for bogus in ("caura_nonexistent", "memclaw_nonexistent"):
        with pytest.raises(ToolError):
            await mcp_server.mcp.call_tool(bogus, {})
