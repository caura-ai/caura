"""The person and the client behind a governance write, in its audit row (p1.09).

Two keys land in ``detail`` on each write the build plan lists — memory delete,
bulk delete, conflict resolve, crystallize, ingest commit and undo, tune, trust,
keystone set and delete — on REST and on MCP:

- ``user_id``: the person the enterprise gateway vouched for. Read from
  ``X-User-ID`` on the gateway path, and only when the gateway secret is
  configured. Without the secret the header is the caller's own, and the trail
  must not name a person on the caller's word.
- ``surface``: the Caura client, from ``X-Caura-Surface``. The set is closed; an
  unknown value is dropped without an error, and nothing authorizes on it. MCP
  rows say ``mcp``, which no header can claim.

Both keys are present even when unknown, so an exporter can tell "not known"
(null) from "written before these fields existed" (no key).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from core_api import auth as auth_mod
from core_api import mcp_server, standalone
from core_api.app import http_exception_handler as _http_exception_handler
from core_api.audit_actor import MCP_SURFACE, SURFACES, parse_surface
from core_api.auth import AuthContext
from core_api.routes import agents, conflicts, crystallizer, keystones, memories
from core_api.schemas import (
    AgentTrustUpdate,
    ConflictResolveRequest,
    IngestCommitRequest,
    SearchProfileUpdate,
)
from core_api.services import organization_settings
from tests._legacy_contracts import LEGACY_API_KEY_FIELD
from tests._mcp_test_helpers import parse_envelope, stub_storage_client

# No asyncio mark: ``asyncio_mode = auto`` runs the async tests, and the mark on
# the sync ones would only warn.
pytestmark = pytest.mark.unit

ACTOR = {"user_id": "user-1", "surface": "prism"}


def _ctx(**kwargs) -> AuthContext:
    """A tenant credential carrying the actor fields, as the gateway path builds it."""
    return AuthContext(tenant_id="t1", user_id="user-1", surface="prism", **kwargs)


def _detail(log: AsyncMock, action: str) -> dict:
    """The ``detail`` of the one audit row written for ``action``."""
    rows = [c.kwargs for c in log.await_args_list if c.kwargs["action"] == action]
    assert len(rows) == 1, [c.kwargs["action"] for c in log.await_args_list]
    return rows[0].get("detail") or {}


def _assert_actor(detail: dict, expected: dict = ACTOR) -> None:
    """Both keys present with the expected values; a missing key shows as absent."""
    assert {k: detail.get(k, "<absent>") for k in expected} == expected


def _assert_version_actor(log: AsyncMock, action: str, sent: dict) -> None:
    """A keystone write tells storage who made it, for the version it records
    (g1.12): the agent its audit row names, and the person the gateway vouched
    for, never a person the body claims."""
    [agent] = [
        c.kwargs["agent_id"]
        for c in log.await_args_list
        if c.kwargs["action"] == action
    ]
    assert (sent["actor_agent_id"], sent["actor_user_id"]) == (agent, "user-1")


# ---------------------------------------------------------------------------
# The header: a closed set, and anything else dropped
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", sorted(SURFACES))
def test_every_listed_surface_is_kept(value):
    assert parse_surface(value) == value


def test_case_and_surrounding_whitespace_are_forgiven():
    assert parse_surface("  Prism ") == "prism"


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "cli",
        # Set by core-api for its own /mcp transport. A REST caller cannot claim it.
        MCP_SURFACE,
        "dashboard,prism",
        "dash board",
        pytest.param("prism\x00", id="nul-byte"),
        pytest.param("x" * 1000, id="1000-chars"),
    ],
)
def test_anything_else_is_dropped(raw):
    assert parse_surface(raw) is None


def test_both_keys_are_present_when_unknown():
    assert AuthContext(tenant_id="t1").audit_actor() == {
        "user_id": None,
        "surface": None,
    }


# ---------------------------------------------------------------------------
# REST: what get_auth_context reads, on which path
# ---------------------------------------------------------------------------


class _Req:
    def __init__(self, headers):
        self.headers = headers
        self.state = SimpleNamespace()


async def _resolve(
    monkeypatch,
    headers,
    *,
    secret=None,
    admin_key=None,
    caura_key=None,
    standalone_mode=False,
):
    monkeypatch.setattr(auth_mod.settings, "gateway_shared_secret", secret)
    monkeypatch.setattr(auth_mod.settings, "is_standalone", standalone_mode)
    monkeypatch.setattr(auth_mod.settings, LEGACY_API_KEY_FIELD, caura_key)
    monkeypatch.setattr(auth_mod, "get_admin_key", lambda: admin_key)
    monkeypatch.setattr(standalone, "get_standalone_tenant_id", lambda: "t1")

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(auth_mod, "_block_if_suppressed", _noop)
    monkeypatch.setattr(auth_mod, "_block_if_any_readable_suppressed", _noop)
    return await auth_mod.get_auth_context(_Req(headers), key=headers.get("x-api-key"))


def _gateway(**headers):
    """A request as the gateway forwards it, secret included."""
    return {"x-tenant-id": "t1", "x-gateway-secret": "gw", **headers}


async def test_behind_the_gateway_secret_the_user_id_is_recorded(monkeypatch):
    ctx = await _resolve(monkeypatch, _gateway(**{"x-user-id": "user-1"}), secret="gw")
    assert ctx.user_id == "user-1"


async def test_without_a_secret_the_user_id_is_the_callers_own_and_dropped(monkeypatch):
    """Path 4 still trusts X-Tenant-ID here, by design. It does not follow that
    the audit trail may name a person nobody vouched for."""
    ctx = await _resolve(monkeypatch, {"x-tenant-id": "t1", "x-user-id": "user-1"})
    assert ctx.tenant_id == "t1"
    assert ctx.user_id is None


async def test_a_blank_user_id_is_none(monkeypatch):
    ctx = await _resolve(monkeypatch, _gateway(**{"x-user-id": "  "}), secret="gw")
    assert ctx.user_id is None


@pytest.mark.parametrize(
    "path",
    [
        pytest.param(
            {"headers": {"x-api-key": "adm"}, "admin_key": "adm"}, id="admin-key"
        ),
        pytest.param(
            {"headers": {"x-api-key": "ck", "x-tenant-id": "t1"}, "caura_key": "ck"},
            id="caura-api-key",
        ),
        pytest.param({"headers": {}, "standalone_mode": True}, id="standalone"),
    ],
)
async def test_no_other_path_reads_the_user_id(monkeypatch, path):
    headers = {**path["headers"], "x-user-id": "user-1", "x-caura-surface": "prism"}
    settings = {k: v for k, v in path.items() if k != "headers"}
    ctx = await _resolve(monkeypatch, headers, **settings)
    assert ctx.user_id is None
    # The surface is read on every path: it names the client, not the caller.
    assert ctx.surface == "prism"


async def test_the_surface_is_read_behind_the_gateway(monkeypatch):
    ctx = await _resolve(
        monkeypatch, _gateway(**{"x-caura-surface": "dashboard"}), secret="gw"
    )
    assert ctx.surface == "dashboard"


async def test_an_unknown_surface_is_dropped_not_refused(monkeypatch):
    ctx = await _resolve(
        monkeypatch, _gateway(**{"x-caura-surface": "cli"}), secret="gw"
    )
    assert ctx.tenant_id == "t1"
    assert ctx.surface is None


# ---------------------------------------------------------------------------
# REST: each listed write records both
# ---------------------------------------------------------------------------


@pytest.fixture
def log(monkeypatch) -> AsyncMock:
    """One audit mock, installed in every route module these tests drive."""
    mock = AsyncMock()
    for module in (agents, conflicts, crystallizer, keystones, memories):
        monkeypatch.setattr(module, "log_action", mock)
    return mock


def _storage(monkeypatch, module, **returns):
    sc = AsyncMock()
    for name, value in returns.items():
        setattr(sc, name, AsyncMock(return_value=value))
    monkeypatch.setattr(module, "get_storage_client", lambda: sc)
    return sc


async def test_memory_delete(monkeypatch, log):
    monkeypatch.setattr(memories, "soft_delete_memory", AsyncMock())
    await memories.delete_memory(uuid4(), tenant_id="t1", agent_id=None, auth=_ctx())
    _assert_actor(_detail(log, "delete"))


async def test_bulk_delete_by_filter(monkeypatch, log):
    _storage(monkeypatch, memories, soft_delete_by_filter=3)
    await memories.delete_all_memories(
        tenant_id="t1",
        fleet_id="f1",
        agent_id=None,
        memory_type=None,
        status=None,
        confirm_scope=None,
        auth=_ctx(),
        body=None,
    )
    detail = _detail(log, "bulk_delete")
    _assert_actor(detail)
    assert detail["count"] == 3


async def test_bulk_delete_by_ids(monkeypatch, log):
    _storage(monkeypatch, memories, soft_delete_by_ids=1)
    await memories.bulk_delete_by_ids(
        body={"tenant_id": "t1", "ids": [str(uuid4())]}, auth=_ctx()
    )
    _assert_actor(_detail(log, "bulk_delete"))


async def test_conflict_resolve(monkeypatch, log):
    _storage(
        monkeypatch,
        conflicts,
        resolve_memory_conflict={
            "id": str(uuid4()),
            "tenant_id": "t1",
            "new_memory_id": str(uuid4()),
            "old_memory_id": str(uuid4()),
            "relationship": "exact_value",
            "review_status": "resolved",
        },
    )
    monkeypatch.setattr(
        conflicts, "_require_trust", AsyncMock(return_value=(2, False, None))
    )
    await conflicts.resolve_conflict(
        "c1",
        ConflictResolveRequest(tenant_id="t1", review_status="resolved"),
        auth=_ctx(is_person=True),
    )
    _assert_actor(_detail(log, "conflict.review"))


async def test_crystallize(monkeypatch, log):
    report_id = uuid4()
    monkeypatch.setattr(
        crystallizer, "start_crystallization", AsyncMock(return_value=report_id)
    )
    monkeypatch.setattr(
        organization_settings,
        "resolve_config",
        AsyncMock(return_value=SimpleNamespace(auto_crystallize_enabled=False)),
    )
    await crystallizer.trigger_crystallization(
        crystallizer.CrystallizeRequest(tenant_id="t1"), auth=_ctx()
    )
    _assert_actor(_detail(log, "crystallize"))
    assert log.await_args.kwargs["resource_id"] == report_id


async def test_ingest_commit(monkeypatch, log):
    async def _resolve_write_agent(agent_id, tenant_id, fleet_id, **_kw):
        return {"agent_id": agent_id, "fleet_id": None, "trust_level": 1}, agent_id

    monkeypatch.setattr(memories, "resolve_write_agent", _resolve_write_agent)
    monkeypatch.setattr(memories, "enforce_fleet_write", AsyncMock(return_value={}))
    monkeypatch.setattr(memories, "bulk_check_and_increment", AsyncMock())
    monkeypatch.setattr(
        memories,
        "ingest_commit",
        AsyncMock(
            return_value={
                "memories_created": 2,
                "skipped_duplicates": 1,
                "errored": 0,
                "run_id": "run-1",
            }
        ),
    )
    body = IngestCommitRequest(
        tenant_id="t1", facts=[{"content": "a"}, {"content": "b"}, {"content": "c"}]
    )
    await memories.ingest_commit_endpoint(
        request=None, body=body, response=None, auth=_ctx()
    )
    detail = _detail(log, "ingest_commit")
    _assert_actor(detail)
    assert (detail["run_id"], detail["count"], detail["skipped_duplicates"]) == (
        "run-1",
        2,
        1,
    )


async def test_ingest_undo(monkeypatch, log):
    _storage(monkeypatch, memories, soft_delete_by_run=4, delete_document=None)
    await memories.ingest_undo_endpoint("run-1", tenant_id="t1", auth=_ctx())
    _assert_actor(_detail(log, "ingest_undo"))


def _agent_row():
    return {
        "id": str(uuid4()),
        "tenant_id": "t1",
        "agent_id": "worker",
        "trust_level": 1,
        "fleet_id": "f1",
        "search_profile": {},
        "created_at": "2026-10-06T00:00:00Z",
    }


async def test_tune(monkeypatch, log):
    row = _agent_row()
    monkeypatch.setattr(agents, "lookup_agent", AsyncMock(return_value=row))
    _storage(monkeypatch, agents, update_search_profile=None, get_agent=row)
    await agents.patch_agent_tune(
        "worker", SearchProfileUpdate(top_k=7), tenant_id="t1", reset=False, auth=_ctx()
    )
    detail = _detail(log, "agent_tune")
    _assert_actor(detail)
    assert detail["changes"] == {"top_k": 7}


async def test_tune_reset(monkeypatch, log):
    row = _agent_row()
    monkeypatch.setattr(agents, "lookup_agent", AsyncMock(return_value=row))
    _storage(monkeypatch, agents, reset_search_profile=True, get_agent=row)
    await agents.patch_agent_tune(
        "worker", SearchProfileUpdate(), tenant_id="t1", reset=True, auth=_ctx()
    )
    detail = _detail(log, "agent_tune")
    _assert_actor(detail)
    assert detail["reset"] is True


async def test_trust(monkeypatch, log):
    row = _agent_row()
    monkeypatch.setattr(agents, "lookup_agent", AsyncMock(return_value=row))
    monkeypatch.setattr(
        agents, "update_trust_level", AsyncMock(return_value={**row, "trust_level": 2})
    )
    await agents.patch_agent_trust(
        "worker", AgentTrustUpdate(trust_level=2), tenant_id="t1", auth=_ctx()
    )
    _assert_actor(_detail(log, "agent_trust_update"))


async def test_keystone_set(monkeypatch, log):
    monkeypatch.setattr(keystones, "_enforce_author_trust", AsyncMock())
    sc = _storage(
        monkeypatch, keystones, get_document=None, upsert_keystone={"id": str(uuid4())}
    )
    body = keystones.KeystoneSetRequest(
        tenant_id="t1",
        doc_id="no-secrets",
        title="No secrets",
        content="Never.",
        scope="tenant",
        weight="med",
        author_user_id="claimed",
    )
    await keystones.upsert_keystone(body=body, x_agent_id=None, auth=_ctx())
    _assert_actor(_detail(log, "keystone.set"))
    [sent] = sc.upsert_keystone.await_args.args
    _assert_version_actor(log, "keystone.set", sent)
    assert sent["author_user_id"] == "claimed"  # the claim stays on the rule


async def test_keystone_delete(monkeypatch, log):
    monkeypatch.setattr(
        keystones, "_require_trust", AsyncMock(return_value=(3, False, None))
    )
    sc = _storage(
        monkeypatch,
        keystones,
        get_document={"data": {"scope": "tenant"}},
        delete_keystone=True,
    )
    await keystones.delete_keystone(
        doc_id="no-secrets", tenant_id="t1", x_agent_id=None, auth=_ctx()
    )
    _assert_actor(_detail(log, "keystone.delete"))
    _assert_version_actor(log, "keystone.delete", sc.delete_keystone.await_args.kwargs)


# ---------------------------------------------------------------------------
# MCP: the middleware's user id, and each MCP twin of a listed write
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_mcp_user_id():
    """The session-scoped event loop shares one context across tests."""
    yield
    mcp_server._user_id_var.set(None)
    mcp_server._tenant_id_var.set(mcp_server._UNAUTH)
    mcp_server._via_gateway_var.set(False)


async def _through_mcp_middleware(
    monkeypatch, headers: dict, *, secret=None
) -> str | None:
    """The user id ``MCPAuthMiddleware`` leaves for the tool it hands off to."""
    from core_api.config import settings

    monkeypatch.setattr(settings, "gateway_shared_secret", secret)
    seen: dict[str, str | None] = {}

    async def _app(scope, receive, send):
        seen["user_id"] = mcp_server._user_id_var.get(None)

    raw = [(k.encode(), v.encode()) for k, v in headers.items()]
    await mcp_server.MCPAuthMiddleware(_app)(
        {"type": "http", "headers": raw}, AsyncMock(), AsyncMock()
    )
    return seen["user_id"]


async def test_mcp_behind_the_gateway_secret_records_the_user_id(monkeypatch):
    assert (
        await _through_mcp_middleware(
            monkeypatch, _gateway(**{"x-user-id": "user-1"}), secret="gw"
        )
        == "user-1"
    )


async def test_mcp_without_a_secret_drops_the_user_id(monkeypatch):
    assert (
        await _through_mcp_middleware(
            monkeypatch, {"x-tenant-id": "t1", "x-user-id": "user-1"}
        )
        is None
    )


async def test_mcp_rows_always_record_the_mcp_surface():
    mcp_server._user_id_var.set("user-1")
    assert mcp_server._audit_actor() == {"user_id": "user-1", "surface": MCP_SURFACE}


MCP_ACTOR = {"user_id": "user-1", "surface": MCP_SURFACE}


@pytest.fixture
def mcp_log(mcp_env) -> AsyncMock:
    mcp_server._user_id_var.set("user-1")
    return mcp_env["service"]("log_action")


async def test_mcp_delete(mcp_env, mcp_log):
    mcp_env["service"]("soft_delete_memory").return_value = None
    await mcp_server.caura_manage(op="delete", memory_id=str(uuid4()))
    _assert_actor(_detail(mcp_log, "delete"), MCP_ACTOR)


async def test_mcp_bulk_delete(mcp_env, mcp_log, monkeypatch):
    stub_storage_client(monkeypatch, soft_delete_by_ids=1)
    await mcp_server.caura_manage(op="bulk_delete", memory_ids=[str(uuid4())])
    _assert_actor(_detail(mcp_log, "bulk_delete"), MCP_ACTOR)


async def test_mcp_tune(mcp_env, mcp_log, monkeypatch):
    async def _resolve_write_agent(agent_id, tenant_id, fleet_id, **_kw):
        return {
            "id": str(uuid4()),
            "agent_id": agent_id,
            "search_profile": {},
        }, agent_id

    monkeypatch.setattr(mcp_server, "resolve_write_agent", _resolve_write_agent)
    stub_storage_client(monkeypatch, update_search_profile=None)
    out = await mcp_server.caura_tune(agent_id="worker", top_k=7)
    assert "search_profile" in parse_envelope(out)
    detail = _detail(mcp_log, "agent_tune")
    _assert_actor(detail, MCP_ACTOR)
    assert detail["changes"] == {"top_k": 7}


async def test_mcp_keystone_set(mcp_env, mcp_log, monkeypatch):
    sc = stub_storage_client(
        monkeypatch, get_document=None, upsert_keystone={"id": str(uuid4())}
    )
    await mcp_server.caura_keystones_set(
        op="set",
        doc_id="no-secrets",
        title="No secrets",
        content="Never.",
        scope="tenant",
        weight="med",
    )
    _assert_actor(_detail(mcp_log, "keystone.set"), MCP_ACTOR)
    [sent] = sc.upsert_keystone.await_args.args
    _assert_version_actor(mcp_log, "keystone.set", sent)


async def test_mcp_keystone_delete(mcp_env, mcp_log, monkeypatch):
    sc = stub_storage_client(
        monkeypatch, get_document={"data": {"scope": "tenant"}}, delete_keystone=True
    )
    await mcp_server.caura_keystones_set(op="delete", doc_id="no-secrets")
    _assert_actor(_detail(mcp_log, "keystone.delete"), MCP_ACTOR)
    _assert_version_actor(
        mcp_log, "keystone.delete", sc.delete_keystone.await_args.kwargs
    )


# ---------------------------------------------------------------------------
# End to end: gateway headers in, both fields in the audit row
# ---------------------------------------------------------------------------


@pytest.fixture
def gateway_app(monkeypatch, log):
    """The memories router behind the REAL get_auth_context, with a gateway
    secret configured, so the request resolves down Path 4."""
    monkeypatch.setattr(auth_mod.settings, "gateway_shared_secret", "gw")
    monkeypatch.setattr(auth_mod.settings, "is_standalone", False)
    monkeypatch.setattr(auth_mod.settings, LEGACY_API_KEY_FIELD, None)
    monkeypatch.setattr(auth_mod, "get_admin_key", lambda: None)

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(auth_mod, "_block_if_suppressed", _noop)
    monkeypatch.setattr(auth_mod, "_block_if_any_readable_suppressed", _noop)
    _storage(monkeypatch, memories, soft_delete_by_ids=1)
    app = FastAPI()
    app.include_router(memories.router, prefix="/api/v1")
    app.add_exception_handler(HTTPException, _http_exception_handler)
    return app


async def _bulk_delete(app, headers):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post(
            "/api/v1/memories/bulk-delete",
            json={"tenant_id": "t1", "ids": [str(uuid4())]},
            headers=_gateway(**headers),
        )


async def test_gateway_request_records_the_person_and_the_client(gateway_app, log):
    resp = await _bulk_delete(
        gateway_app, {"x-user-id": "user-1", "x-caura-surface": "prism"}
    )
    assert resp.status_code == 200, resp.text
    _assert_actor(_detail(log, "bulk_delete"))


async def test_gateway_request_with_an_unknown_surface_still_succeeds(gateway_app, log):
    resp = await _bulk_delete(
        gateway_app, {"x-user-id": "user-1", "x-caura-surface": "cli"}
    )
    assert resp.status_code == 200, resp.text
    _assert_actor(_detail(log, "bulk_delete"), {"user_id": "user-1", "surface": None})
