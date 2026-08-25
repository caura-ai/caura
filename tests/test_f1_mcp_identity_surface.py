"""What the MCP tool surface tells a caller about its own identity - F1 / SAFE-04A.

``agent_id`` decides which agent a write is attributed to and which rows an
agent-scoped read can see. All ten tools that accept it documented it as
"Caller agent." while defaulting to ``mcp-agent`` - a value the hosted gateway
path actively refuses. The schema therefore advertised a default that cannot be
used where most callers run, and offered no hint of what to send instead.

The default is deliberately kept: ``mcp-agent`` is a legitimate standalone
identity (see ``core_api.agent_ids``), and dropping it would break every
single-tenant caller and change the schema for all of them. Only the
description changes, and these tests pin both halves of that - the text now
says something useful, AND the default did not move.

The guard's message is the second half. It fires on eleven tools, seven of them
reads, while opening with "Writes via the gateway ..." - a sentence that is
simply false on a read, and the same class of error that teaches an agent a
wrong general rule.
"""

from __future__ import annotations

import inspect
import json
from contextlib import contextmanager
import typing

import pytest

from core_api import mcp_server
from core_api.agent_ids import DEFAULT_AGENT_ID

pytestmark = pytest.mark.unit

# Tools whose signature exposes agent_id to the client.
AGENT_ID_TOOLS = [
    "caura_recall", "caura_write", "caura_manage", "caura_tune", "caura_doc",
    "caura_list", "caura_stats", "caura_insights", "caura_evolve", "caura_keystones",
]


def _agent_id_field(tool_name: str):
    fn = getattr(mcp_server, tool_name)
    hints = typing.get_type_hints(fn, include_extras=True)
    annotated = hints["agent_id"]
    return typing.get_args(annotated)[1]


def _default(tool_name: str):
    return inspect.signature(getattr(mcp_server, tool_name)).parameters["agent_id"].default


@pytest.mark.parametrize("tool", AGENT_ID_TOOLS)
def test_the_schema_explains_what_agent_id_decides(tool: str) -> None:
    description = (_agent_id_field(tool).description or "").lower()
    assert description != "caller agent."
    # It governs two different things and the old text implied neither.
    assert "attributed" in description
    assert "read" in description


@pytest.mark.parametrize("tool", AGENT_ID_TOOLS)
def test_the_schema_warns_that_the_default_is_refused_when_hosted(tool: str) -> None:
    """The concrete trap: the advertised default cannot be used on the gateway.

    Asserted on meaning rather than exact wording, because this text is paid on
    every agent call and will be re-tightened whenever the token ceiling bites.
    """
    description = (_agent_id_field(tool).description or "").lower()
    assert "hosted" in description
    assert "real one" in description or "real value" in description


@pytest.mark.parametrize("tool", AGENT_ID_TOOLS)
def test_the_default_itself_did_not_move(tool: str) -> None:
    """Standalone depends on it. This is a documentation fix, not a contract change."""
    assert _default(tool) == DEFAULT_AGENT_ID == "mcp-agent"


@contextmanager
def gateway_without_agent_identity():
    """The tenant-key-through-the-gateway case the guard exists for.

    ``_via_gateway_var`` is a ContextVar, so it is set and reset rather than
    monkeypatched - attributes cannot be assigned on one.
    """
    token = mcp_server._via_gateway_var.set(True)
    agent_token = mcp_server._agent_id_var.set(None)
    try:
        yield
    finally:
        mcp_server._via_gateway_var.reset(token)
        mcp_server._agent_id_var.reset(agent_token)


def test_the_gateway_guard_does_not_call_every_call_a_write() -> None:
    """Seven of the eleven guarded tools are reads."""
    with gateway_without_agent_identity():
        envelope = mcp_server._refuse_default_agent_on_gateway(DEFAULT_AGENT_ID)
    assert envelope is not None
    message = json.loads(envelope)["error"]["message"]

    assert not message.startswith("Writes")
    # It must say why a read is affected too, or a reader reasonably concludes
    # reads are exempt and goes hunting the wrong problem.
    assert "read" in message.lower()
    # And it must still name the way out - the part that already worked.
    assert "agent_id=<your-agent-name>" in message
    assert "agent-scoped credential" in message


def test_the_guard_still_lets_legitimate_callers_through() -> None:
    """Three ways to be legitimate: off-gateway, identity already resolved, or
    a real id supplied. Widening the message must not widen the refusal."""
    # Off the gateway entirely - standalone, where mcp-agent is by design.
    assert mcp_server._refuse_default_agent_on_gateway(DEFAULT_AGENT_ID) is None

    with gateway_without_agent_identity():
        # A real id supplied by the caller.
        assert mcp_server._refuse_default_agent_on_gateway("a-real-agent") is None

    # Gateway injected an identity (agent-scoped credential).
    token = mcp_server._via_gateway_var.set(True)
    agent_token = mcp_server._agent_id_var.set("resolved-agent")
    try:
        assert mcp_server._refuse_default_agent_on_gateway(DEFAULT_AGENT_ID) is None
    finally:
        mcp_server._via_gateway_var.reset(token)
        mcp_server._agent_id_var.reset(agent_token)


def test_the_error_code_is_unchanged() -> None:
    """MISSING_AGENT_ID is the documented contract and A14/A29 closed against it."""
    token = mcp_server._via_gateway_var.set(True)
    try:
        envelope = mcp_server._refuse_default_agent_on_gateway(DEFAULT_AGENT_ID)
    finally:
        mcp_server._via_gateway_var.reset(token)
    assert envelope is not None
    assert json.loads(envelope)["error"]["code"] == "MISSING_AGENT_ID"
