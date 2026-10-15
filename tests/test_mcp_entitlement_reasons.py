"""Trusted license/organization verdicts cannot be disabled by usage rollout."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from core_api import mcp_server
from core_api.config import settings
from tests._mcp_test_helpers import (
    is_error_envelope,
    parse_envelope,
    stub_storage_client,
)

pytestmark = pytest.mark.unit

MEMORY = "22222222-2222-2222-2222-222222222222"


@pytest.mark.parametrize("reason", ["license", "organization"])
async def test_registered_mcp_call_preserves_entitlement_error_envelope(
    mcp_env, reason
):
    mcp_server._org_read_only_var.set(True)
    mcp_server._org_read_only_reason_var.set(reason)
    result = await mcp_server.mcp.call_tool(
        "caura_write", {"content": "new memory", "agent_id": "synthetic-agent"}
    )
    assert is_error_envelope(result)
    assert parse_envelope(result)["error"]["code"] == "PLAN_LIMIT_READ_ONLY"


@pytest.mark.parametrize("reason", ["organization", None])
async def test_a_plan_limit_refusal_never_offers_deleting_data(
    mcp_env, monkeypatch, reason
):
    """ENT2-095: read-only mode compares the month's operation counters, which
    deleting data does not lower, so the refusal does not offer it."""
    monkeypatch.setattr(settings, "enforce_mcp_plan_limits", True)
    mcp_server._org_read_only_var.set(True)
    mcp_server._org_read_only_reason_var.set(reason)
    result = await mcp_server.mcp.call_tool(
        "caura_write", {"content": "new memory", "agent_id": "synthetic-agent"}
    )
    error = parse_envelope(result)["error"]
    assert error["code"] == "PLAN_LIMIT_READ_ONLY"
    assert "delete data" not in error["message"]
    assert "monthly" in error["message"]


@pytest.fixture(autouse=True)
def _entitlement_context(monkeypatch):
    defaults = [
        (mcp_server._tenant_id_var, mcp_server._UNAUTH),
        (mcp_server._agent_id_var, None),
        (mcp_server._readable_tenant_ids_var, None),
        (mcp_server._scopes_var, None),
        (mcp_server._via_gateway_var, False),
        (mcp_server._credential_kind_var, None),
        (mcp_server._install_uuid_var, None),
        (mcp_server._user_id_var, None),
        (mcp_server._org_read_only_var, False),
        (mcp_server._org_read_only_unknown_var, False),
        (mcp_server._org_read_only_reason_var, None),
    ]
    tokens = [(var, var.set(value)) for var, value in defaults]
    monkeypatch.setattr(settings, "enforce_mcp_plan_limits", False)
    yield
    for var, token in reversed(tokens):
        var.reset(token)


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("caura_write", {"content": "new memory"}),
        ("caura_write", {"items": [{"content": "new memory"}]}),
        ("caura_manage", {"op": "update", "memory_id": MEMORY, "content": "new"}),
        (
            "caura_manage",
            {"op": "transition", "memory_id": MEMORY, "status": "archived"},
        ),
        ("caura_manage", {"op": "delete", "memory_id": MEMORY}),
        ("caura_manage", {"op": "bulk_delete", "memory_ids": [MEMORY]}),
        ("caura_tune", {"top_k": 10}),
        ("caura_tune", {"reset": True}),
        (
            "caura_doc",
            {"op": "write", "collection": "things", "doc_id": "d1", "data": {"k": "v"}},
        ),
        ("caura_doc", {"op": "delete", "collection": "things", "doc_id": "d1"}),
        ("caura_insights", {"focus": "patterns"}),
        ("caura_evolve", {"outcome": "x", "outcome_type": "success"}),
        (
            "caura_keystones_set",
            {
                "op": "set",
                "doc_id": "rule-1",
                "title": "t",
                "content": "c",
                "scope": "tenant",
                "weight": "low",
            },
        ),
        ("caura_keystones_set", {"op": "delete", "doc_id": "rule-1"}),
    ],
)
async def test_license_blocks_each_mutation_before_service_work(
    mcp_env, monkeypatch, tool, arguments
):
    mcp_server._org_read_only_var.set(True)
    mcp_server._org_read_only_reason_var.set("license")
    monkeypatch.setattr(mcp_server, "_get_scopes", lambda: {"read", "write"})
    storage = stub_storage_client(monkeypatch)
    result = await getattr(mcp_server, tool)(**arguments)
    assert is_error_envelope(result)
    error = parse_envelope(result)["error"]
    assert error["code"] == "PLAN_LIMIT_READ_ONLY"
    assert "license" in error["message"]
    assert not mcp_env["service_mocks"]
    assert not storage.mock_calls


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("caura_write", {"content": "new memory", "agent_id": "synthetic-agent"}),
        (
            "caura_write",
            {"items": [{"content": "new memory"}], "agent_id": "synthetic-agent"},
        ),
        (
            "caura_doc",
            {
                "op": "write",
                "collection": "things",
                "doc_id": "d1",
                "data": {"k": "v"},
                "agent_id": "synthetic-agent",
            },
        ),
        ("caura_insights", {"focus": "patterns", "agent_id": "synthetic-agent"}),
        (
            "caura_evolve",
            {"outcome": "x", "outcome_type": "success", "agent_id": "synthetic-agent"},
        ),
        (
            "caura_keystones_set",
            {
                "op": "set",
                "doc_id": "rule-1",
                "title": "t",
                "content": "c",
                "scope": "tenant",
                "weight": "low",
                "caller_agent_id": "synthetic-agent",
            },
        ),
    ],
)
async def test_org_verdict_blocks_real_policy_gated_tools_with_rollout_off(
    mcp_env, monkeypatch, tool, arguments
):
    mcp_server._org_read_only_var.set(True)
    mcp_server._org_read_only_reason_var.set("organization")
    mcp_server._via_gateway_var.set(False)
    storage = stub_storage_client(monkeypatch)
    result = await getattr(mcp_server, tool)(**arguments)
    assert is_error_envelope(result)
    assert parse_envelope(result)["error"]["code"] == "PLAN_LIMIT_READ_ONLY"
    assert not storage.mock_calls


@pytest.mark.parametrize(
    "op", ["create", "bulk_create", "doc_write", "insights", "evolve", "keystone_set"]
)
def test_authoritative_organization_verdict_refuses_rest_gated_operations(op):
    mcp_server._org_read_only_var.set(True)
    mcp_server._org_read_only_reason_var.set("organization")
    result = mcp_server._check_plan_limit(op, "test-tenant")
    assert result is not None
    assert parse_envelope(result)["error"]["code"] == "PLAN_LIMIT_READ_ONLY"


@pytest.mark.parametrize("op", ["update", "transition", "delete", "bulk_delete"])
def test_organization_quota_cleanup_policy_stays_available(op):
    mcp_server._org_read_only_var.set(True)
    mcp_server._org_read_only_reason_var.set("organization")
    assert mcp_server._check_plan_limit(op, "test-tenant") is None


async def _request(monkeypatch, headers):
    captured = []
    monkeypatch.setattr(mcp_server, "get_admin_key", lambda: None)
    monkeypatch.setattr(
        settings,
        "memclaw_api_key",  # legacy-name-ok: resolved shared-key compatibility field
        "",
    )
    monkeypatch.setattr(
        mcp_server, "_refuse_if_suppressed", AsyncMock(return_value=False)
    )

    async def app(scope, receive, send):
        captured.append(
            (
                mcp_server._org_read_only_reason_var.get(),
                mcp_server._check_write_scope(),
            )
        )

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    await mcp_server.MCPAuthMiddleware(app)(
        {"type": "http", "headers": headers}, receive, AsyncMock()
    )
    return captured


async def test_verified_middleware_reason_blocks_then_resets_on_next_request(
    monkeypatch,
):
    monkeypatch.setattr(settings, "gateway_shared_secret", "synthetic-gateway")
    base = [
        (b"x-tenant-id", b"test-tenant"),
        (b"x-gateway-secret", b"synthetic-gateway"),
    ]
    blocked = await _request(
        monkeypatch,
        [*base, (b"x-org-read-only", b"true"), (b"x-org-read-only-reason", b"license")],
    )
    assert blocked[0][0] == "license"
    assert is_error_envelope(blocked[0][1])
    cleared = await _request(monkeypatch, base)
    assert cleared == [(None, None)]


@pytest.mark.parametrize("verdict", [b"false", b"unknown", b""])
async def test_reason_without_an_affirmative_verdict_is_not_authoritative(
    monkeypatch, verdict
):
    monkeypatch.setattr(settings, "gateway_shared_secret", "synthetic-gateway")
    captured = await _request(
        monkeypatch,
        [
            (b"x-tenant-id", b"test-tenant"),
            (b"x-gateway-secret", b"synthetic-gateway"),
            (b"x-org-read-only", verdict),
            (b"x-org-read-only-reason", b"license"),
        ],
    )
    assert captured == [(None, None)]


async def test_unconfigured_gateway_cannot_supply_a_hard_entitlement_reason(
    monkeypatch,
):
    monkeypatch.setattr(settings, "gateway_shared_secret", "")
    captured = await _request(
        monkeypatch,
        [
            (b"x-tenant-id", b"test-tenant"),
            (b"x-org-read-only", b"true"),
            (b"x-org-read-only-reason", b"license"),
        ],
    )
    assert captured == [(None, None)]


async def test_license_does_not_block_reading_existing_memory(mcp_env, monkeypatch):
    mcp_server._org_read_only_var.set(True)
    mcp_server._org_read_only_reason_var.set("license")
    row = {
        "id": MEMORY,
        "tenant_id": "test-tenant",
        "agent_id": "mcp-agent",
        "fleet_id": None,
        "memory_type": "fact",
        "status": "active",
        "visibility": "scope_team",
        "content": "historical data",
        "title": "t",
        "summary": "s",
        "weight": 0.5,
        "metadata_": {},
    }
    storage = stub_storage_client(monkeypatch, get_memory=row)
    monkeypatch.setattr(
        mcp_server, "authorize_memory_access", AsyncMock(return_value=True)
    )
    result = await mcp_server.caura_manage(op="read", memory_id=MEMORY)
    assert not is_error_envelope(result)
    assert parse_envelope(result)["content"] == "historical data"
    storage.get_memory.assert_awaited_once()


async def test_wrong_gateway_secret_cannot_admit_a_reason(monkeypatch):
    monkeypatch.setattr(settings, "gateway_shared_secret", "synthetic-gateway")
    captured = await _request(
        monkeypatch,
        [
            (b"x-tenant-id", b"test-tenant"),
            (b"x-gateway-secret", b"wrong-secret"),
            (b"x-org-read-only", b"true"),
            (b"x-org-read-only-reason", b"license"),
        ],
    )
    assert captured == []


async def test_unknown_reason_keeps_legacy_boolean_rollout_behavior(monkeypatch):
    monkeypatch.setattr(settings, "gateway_shared_secret", "synthetic-gateway")
    captured = await _request(
        monkeypatch,
        [
            (b"x-tenant-id", b"test-tenant"),
            (b"x-gateway-secret", b"synthetic-gateway"),
            (b"x-org-read-only", b"true"),
            (b"x-org-read-only-reason", b"unrecognized"),
        ],
    )
    assert captured == [(None, None)]
