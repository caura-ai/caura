"""Refusals that tell someone to get a key say who makes keys (Prism App Review v2 R3).

On caura.ai, keys are made by people with the Manage API credentials permission,
and everyone else asks an organization admin. Three refusals said otherwise:
MCP's missing-agent error named a dashboard "Create agent key" form nobody has
any more, and the read-only refusals (MCP and REST) sent everyone to Connect >
Keys to make a key with write.

Pins, for each of the three: it names the permission and sends anyone without
it to an admin; and a mention of the managed provisioning route stays marked as
caura.ai's, as the docs must (``test_l157`` in ``test_docs_match_the_code.py``).
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from core_api import mcp_server
from core_api.auth import AuthContext

pytestmark = pytest.mark.unit


def _missing_agent_message() -> str:
    token = mcp_server._via_gateway_var.set(True)
    try:
        envelope = mcp_server._refuse_default_agent_on_gateway(
            mcp_server._DEFAULT_AGENT_ID
        )
    finally:
        mcp_server._via_gateway_var.reset(token)
    assert envelope is not None
    return json.loads(envelope)["error"]["message"]


def _mcp_read_only_message() -> str:
    return json.loads(mcp_server._READ_ONLY_ERROR.content[0].text)["error"]["message"]


def _rest_read_only_key_remediation() -> str:
    with pytest.raises(HTTPException) as caught:
        AuthContext(tenant_id="t", capabilities={"read"}).enforce_read_only()
    return caught.value.detail["details"]["remediation"]


REFUSALS = {
    "mcp-missing-agent": _missing_agent_message,
    "mcp-read-only": _mcp_read_only_message,
    "rest-read-only-key": _rest_read_only_key_remediation,
}


@pytest.mark.parametrize("name", REFUSALS)
def test_says_who_makes_keys_and_to_ask_an_admin(name: str) -> None:
    text = REFUSALS[name]()
    assert "Manage API credentials" in text
    assert "ask an organization admin" in text


def test_the_missing_agent_error_names_no_retired_dashboard_form() -> None:
    assert "Create agent key" not in _missing_agent_message()


@pytest.mark.parametrize("name", REFUSALS)
def test_the_managed_provisioning_route_stays_marked_as_caura_ai(name: str) -> None:
    text = REFUSALS[name]()
    if "agent-keys/provision" in text:
        assert "caura.ai" in text
