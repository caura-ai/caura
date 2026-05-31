"""A14: every MCP write tool must refuse the literal ``"mcp-agent"`` default
on gateway-routed requests (tenant-key holder, no X-Agent-ID injection).

The existing memclaw_write coverage lives in tests/test_mcp_write.py;
this file extends the policy to the remaining write surfaces that
were missed by PR #139 — memclaw_manage, memclaw_tune, memclaw_evolve,
and the internal caller-identity fallback in memclaw_keystones_set.
"""

from __future__ import annotations

import uuid

import pytest

from core_api import mcp_server
from tests._mcp_test_helpers import parse_envelope

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.mark.parametrize(
    "tool_name,kwargs",
    [
        # memclaw_manage: write op (delete) with a valid UUID so we reach the guard.
        ("memclaw_manage", {"op": "delete", "memory_id": str(uuid.uuid4())}),
        # memclaw_tune: every param optional; guard is the first business logic step.
        ("memclaw_tune", {}),
        # memclaw_evolve: outcome + outcome_type required for arg validation.
        ("memclaw_evolve", {"outcome": "any", "outcome_type": "success"}),
        # memclaw_keystones_set: op + doc_id required to clear arg validation;
        # the guard fires on the internal caller-identity fallback, not the
        # schema param (which is the TARGET agent and defaults to None).
        ("memclaw_keystones_set", {"op": "delete", "doc_id": "any-rule"}),
    ],
)
async def test_write_tool_refuses_default_agent_on_gateway(mcp_env, tool_name, kwargs):
    """Gateway-routed + tenant-key (no X-Agent-ID injected) + caller relies on
    the default identity → MISSING_AGENT_ID, no downstream side-effect."""
    tool = getattr(mcp_server, tool_name)

    token = mcp_server._via_gateway_var.set(True)
    try:
        out = await tool(**kwargs)
    finally:
        mcp_server._via_gateway_var.reset(token)

    payload = parse_envelope(out)
    assert payload["error"]["code"] == "MISSING_AGENT_ID", (
        f"{tool_name} did not refuse the default identity on gateway path; "
        f"got {payload!r}"
    )
