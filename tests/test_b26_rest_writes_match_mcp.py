"""REST writes answer as their MCP twins do (the REST half of audit batch B26).

Each test names the finding it pins: L-119 (clearing one tuned knob), L-121 (a
document re-written without a summary leaves search) and L-128 (evolve and
insights refuse a request that names no agent).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.exceptions import RequestValidationError

from core_api import mcp_server
from core_api.services import caller_identity
from core_api.services.caller_identity import resolve_caller_and_gate
from tests._mcp_test_helpers import parse_envelope, stub_storage_client
from tests.conftest import get_test_auth
from tests.conftest import uid as _uid

# --- L-119: one knob can be cleared without the others ---------------------------


async def _tuned_agent(client, sc, headers, tenant_id) -> str:
    agent_id = f"tuner-{_uid()}"
    await sc.create_or_update_agent(
        {
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "fleet_id": f"fleet-{_uid()}",
            "trust_level": 1,
        }
    )
    resp = await client.patch(
        f"/api/v1/agents/{agent_id}/tune",
        params={"tenant_id": tenant_id},
        json={"top_k": 10, "min_similarity": 0.5},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return agent_id


async def _reset(client, headers, tenant_id, agent_id, *, body=None, **params):
    return await client.patch(
        f"/api/v1/agents/{agent_id}/tune",
        params={"tenant_id": tenant_id, **params},
        json=body or {},
        headers=headers,
    )


@pytest.mark.integration
async def test_l119_rest_clears_the_named_knob_and_keeps_the_rest(client, sc):
    tenant_id, headers = get_test_auth()
    agent_id = await _tuned_agent(client, sc, headers, tenant_id)

    resp = await _reset(client, headers, tenant_id, agent_id, reset_knobs="top_k")

    assert resp.status_code == 200, resp.text
    assert resp.json()["search_profile"] == {"min_similarity": 0.5}


@pytest.mark.integration
@pytest.mark.parametrize(
    ("body", "params"),
    [
        # Not a tunable knob.
        (None, {"reset_knobs": "bogus"}),
        # Set and cleared in one request: which one was meant?
        ({"top_k": 7}, {"reset_knobs": "top_k"}),
        # ``reset`` already clears every knob.
        (None, {"reset_knobs": "top_k", "reset": "true"}),
    ],
    ids=["unknown-knob", "set-and-cleared", "with-reset"],
)
async def test_l119_rest_refuses_a_reset_it_cannot_carry_out(client, sc, body, params):
    tenant_id, headers = get_test_auth()
    agent_id = await _tuned_agent(client, sc, headers, tenant_id)

    resp = await _reset(client, headers, tenant_id, agent_id, body=body, **params)

    assert resp.status_code == 422, resp.text


@pytest.mark.unit
async def test_l119_mcp_clears_the_named_knob_and_keeps_the_rest(mcp_env, monkeypatch):
    agent = {
        "id": "row-1",
        "agent_id": "alice",
        "search_profile": {"top_k": 10, "min_similarity": 0.5},
    }
    monkeypatch.setattr(
        mcp_server, "resolve_write_agent", AsyncMock(return_value=(agent, "alice"))
    )
    sc = stub_storage_client(monkeypatch, update_search_profile=None)
    monkeypatch.setattr(mcp_server, "log_action", AsyncMock())

    out = await mcp_server.caura_tune(agent_id="alice", reset_knobs=["top_k"])

    assert parse_envelope(out)["search_profile"] == {"min_similarity": 0.5}
    sent = sc.update_search_profile.await_args.args
    assert sent[2] == {"min_similarity": 0.5}


@pytest.mark.unit
@pytest.mark.parametrize(
    "kwargs",
    [
        {"reset_knobs": ["bogus"]},
        {"reset_knobs": ["top_k"], "top_k": 7},
        {"reset_knobs": ["top_k"], "reset": True},
    ],
    ids=["unknown-knob", "set-and-cleared", "with-reset"],
)
async def test_l119_mcp_refuses_a_reset_it_cannot_carry_out(
    mcp_env, monkeypatch, kwargs
):
    stub_storage_client(monkeypatch)

    out = await mcp_server.caura_tune(agent_id="alice", **kwargs)

    assert parse_envelope(out)["error"]["code"] == "INVALID_ARGUMENTS"


# --- L-121: a re-write without a summary leaves document search --------------------


@pytest.mark.integration
async def test_l121_a_rewrite_without_a_summary_is_no_longer_searchable(client):
    tenant_id, headers = get_test_auth()
    collection = f"l121-{_uid()}"
    doc = {"tenant_id": tenant_id, "collection": collection, "doc_id": "runbook"}
    summary = f"rotate the edge certificates {_uid()}"

    first = await client.post(
        "/api/v1/documents", json={**doc, "data": {"summary": summary}}, headers=headers
    )
    assert first.status_code == 200, first.text
    assert first.json()["indexed"] is True, "the fixture never indexed the document"

    second = await client.post(
        "/api/v1/documents",
        json={**doc, "data": {"steps": "renew, then restart the proxies"}},
        headers=headers,
    )
    assert second.status_code == 200, second.text
    assert second.json()["indexed"] is False

    # The query IS the old summary, so the stale vector would score 1.0.
    found = await client.post(
        "/api/v1/documents/search",
        json={"tenant_id": tenant_id, "collection": collection, "query": summary},
        headers=headers,
    )
    assert found.status_code == 200, found.text
    assert found.json()["items"] == [], (
        "a document re-written without a summary still matched its old summary"
    )


# --- L-128: evolve and insights refuse a request that names no agent ---------------


def _auth(*, agent_id=None, is_install_credential=False):
    return SimpleNamespace(
        agent_id=agent_id,
        is_admin=False,
        is_install_credential=is_install_credential,
        install_uuid="install-1" if is_install_credential else None,
    )


@pytest.mark.unit
@pytest.mark.parametrize("action", ["evolve", "insights"])
@pytest.mark.parametrize(
    "auth", [_auth(), _auth(is_install_credential=True)], ids=["tenant", "install"]
)
async def test_l128_a_credential_without_an_agent_must_name_one(
    monkeypatch, action, auth
):
    # Outside standalone; the test env runs IS_STANDALONE=true.
    monkeypatch.setattr(caller_identity.app_settings, "is_standalone", False)
    # Registered at full trust, so the shared placeholder would be accepted.
    monkeypatch.setattr(
        caller_identity, "require_trust", AsyncMock(return_value=(3, False, None))
    )
    monkeypatch.setattr(
        caller_identity, "broker_owned_agent_id", AsyncMock(side_effect=lambda a, *_: a)
    )

    with pytest.raises(RequestValidationError) as exc:
        await resolve_caller_and_gate(
            auth, tenant_id="t", body_agent_id=None, scope="agent", action=action
        )

    [error] = exc.value.errors()
    assert error["loc"] == ("body", "agent_id")


@pytest.mark.unit
async def test_l128_a_named_agent_still_resolves(monkeypatch):
    monkeypatch.setattr(caller_identity.app_settings, "is_standalone", False)
    monkeypatch.setattr(
        caller_identity, "require_trust", AsyncMock(return_value=(1, False, None))
    )

    result = await resolve_caller_and_gate(
        _auth(), tenant_id="t", body_agent_id="alice", scope="agent", action="evolve"
    )

    assert result == "alice"
