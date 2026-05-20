"""End-to-end tests that exercise ``mcp.call_tool(...)`` — the FastMCP
surface used by every real MCP client.

These tests close the gap that let the structured-output-schema bug ship:
the rest of the unit suite invokes handler coroutines directly (e.g.
``await mcp_server.memclaw_write(...)``), which skips
``FuncMetadata.convert_result`` and never exercises FastMCP's output-schema
validation. The bug lived there: when a tool registered with
``structured_output`` enabled returned a ``CallToolResult`` on its error
path, ``convert_result`` called ``output_model.model_validate(
result.structuredContent)`` against the ``None`` default and raised
``"1 validation error for <tool>Output ... input_value=None"`` — masking
the legitimate error envelope.

The ``call_tool`` path matches what JSON-RPC ``tools/call`` runs, so a
green test here is the same guarantee a remote MCP client gets.
"""

from __future__ import annotations

import pytest

from core_api import mcp_server
from tests._mcp_test_helpers import as_text, parse_envelope

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_auth_error_returns_clean_envelope(monkeypatch):
    """Regression test for the structured-output mismatch.

    ``_check_auth`` returns a pre-baked ``CallToolResult(isError=True)``
    on auth failure. Pre-fix, ``mcp.call_tool`` raised
    ``ToolError("Error executing tool memclaw_write: 1 validation error
    for memclaw_writeOutput ... input_value=None")`` and the legitimate
    UNAUTHORIZED envelope was lost. Post-fix
    (``structured_output=False`` on registration), the CallToolResult
    flows through ``convert_result`` untouched.
    """
    monkeypatch.setattr(mcp_server, "_check_auth", lambda: mcp_server._AUTH_ERROR)

    result = await mcp_server.mcp.call_tool(
        "memclaw_write",
        {"content": "test memory body", "agent_id": "claude-eldad"},
    )

    assert "validation error" not in as_text(result), (
        "FastMCP's output-schema validation leaked into the response. "
        "Re-enable `structured_output=False` on `mcp_register` "
        "(core_api/tools/_builders.py)."
    )
    envelope = parse_envelope(result)
    assert envelope["error"]["code"] == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_admin_key_refusal_returns_clean_envelope(monkeypatch):
    """Same regression coverage for ``_ADMIN_ERROR`` — the other module-level
    pre-baked CallToolResult that ``_check_auth`` can hand back.
    """
    monkeypatch.setattr(mcp_server, "_check_auth", lambda: mcp_server._ADMIN_ERROR)

    result = await mcp_server.mcp.call_tool(
        "memclaw_keystones_set",
        {
            "op": "set",
            "doc_id": "probe",
            "title": "probe",
            "content": "probe",
            "scope": "agent",
            "agent_id": "claude-eldad",
            "weight": "low",
        },
    )

    assert "validation error" not in as_text(result)
    envelope = parse_envelope(result)
    assert envelope["error"]["code"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_invalid_args_returns_clean_envelope(monkeypatch):
    """The ``_with_latency(_error_response(...))`` → ``_as_error_result``
    path is the other major source of CallToolResult returns. Calling
    ``memclaw_write`` with neither ``content`` nor ``items`` triggers it
    via the in-handler INVALID_ARGUMENTS branch.
    """
    monkeypatch.setattr(mcp_server, "_check_auth", lambda: None)

    result = await mcp_server.mcp.call_tool("memclaw_write", {})

    assert "validation error" not in as_text(result)
    envelope = parse_envelope(result)
    assert envelope["error"]["code"] == "INVALID_ARGUMENTS"


@pytest.mark.asyncio
async def test_no_tool_has_structured_output_schema():
    """Belt-and-braces guard: if a future contributor flips
    ``structured_output`` back on (or registers a tool that bypasses
    ``mcp_register``), this test fails before the bug ships. Without
    ``structured_output=False`` every tool gets an auto-generated
    ``{result: str}`` output schema that rejects ``CallToolResult``
    returns at ``convert_result`` time.
    """
    tools = await mcp_server.mcp.list_tools()
    offenders = [t.name for t in tools if t.outputSchema is not None]
    assert not offenders, (
        f"Tools with an output schema present: {offenders}. The unified "
        f"error-envelope contract requires `structured_output=False` on "
        f"every registration — see core_api/tools/_builders.py:mcp_register."
    )
