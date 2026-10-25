"""Audit access, payload and verification contracts at the API boundary."""

import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from core_api import auth as auth_mod
from core_api.auth import AuthContext, get_auth_context
from core_api.routes import audit
from core_api.services import audit_service

pytestmark = pytest.mark.unit


@pytest.fixture
def api(monkeypatch):
    for name in ("_verification_inflight", "_verification_next_allowed"):
        state = getattr(audit, name, None)
        if state is not None:
            state.clear()
    ctx = AuthContext(tenant_id="t1")
    sc = SimpleNamespace(
        list_audit_logs=AsyncMock(return_value=[]),
        verify_audit_chain=AsyncMock(
            return_value={"tenant_id": "t1", "valid": True, "verified_count": 0}
        ),
    )
    monkeypatch.setattr(audit, "get_storage_client", lambda: sc)
    app = FastAPI()
    app.include_router(audit.router)
    app.dependency_overrides[get_auth_context] = lambda: ctx
    return app, ctx, sc


@pytest.mark.parametrize("install", [False, True])
async def test_machine_credentials_cannot_list_audit(api, install):
    app, ctx, sc = api
    ctx.agent_id = None if install else "peer"
    ctx.is_install_credential = install
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/audit-log", params={"tenant_id": "t1"})
    assert response.status_code == 403
    sc.list_audit_logs.assert_not_awaited()


async def test_generic_audit_drops_excerpts_without_hashing_unchecked_text(monkeypatch):
    post = AsyncMock()
    monkeypatch.setattr(audit_service, "get_audit_queue", lambda: None)
    monkeypatch.setattr(audit_service, "_post_audit_sync", post)
    await audit_service.log_action(
        tenant_id="t1",
        action="update",
        resource_type="memory",
        detail={
            "changes": {
                "content": {"old": "old private text", "new": "new private text"}
            },
            "content_changed": True,
        },
    )
    detail = post.call_args.args[0]["detail"]
    assert "content" not in detail["changes"]
    assert detail["content_changed"] is True
    assert "content_sha256" not in detail


async def test_cross_tenant_audit_drops_query_text(monkeypatch):
    post = AsyncMock()
    monkeypatch.setattr(audit_service, "get_audit_queue", lambda: None)
    monkeypatch.setattr(audit_service, "_post_audit_sync", post)
    await audit_service.log_cross_tenant_read(
        home_tenant_id="t1",
        home_agent_id="peer",
        source_tenants=["t2"],
        surface="search",
        query_summary="private query",
    )
    assert "query_summary" not in post.call_args.args[0]["detail"]


@pytest.mark.parametrize(
    "agent,user", [(None, None), (None, "person-1"), ("real-agent", None)]
)
async def test_audit_uses_authenticated_actor_and_credential(monkeypatch, agent, user):
    ctx = AuthContext(
        tenant_id="t1", agent_id=agent, agent_id_verified=True, user_id=user
    )
    monkeypatch.setattr(auth_mod, "_resolve_auth_context", AsyncMock(return_value=ctx))
    request = SimpleNamespace(
        headers={
            "x-caura-credential-id": "cred-1",
            "x-caura-credential-kind": "agent_key" if agent else "user_api_key",
        }
    )
    monkeypatch.setattr(auth_mod.settings, "gateway_shared_secret", "test-gateway")
    await get_auth_context(request, key=None)
    post = AsyncMock()
    monkeypatch.setattr(audit_service, "get_audit_queue", lambda: None)
    monkeypatch.setattr(audit_service, "_post_audit_sync", post)
    await audit_service.log_action(
        tenant_id="t1",
        agent_id="claimed-agent",
        action="update",
        resource_type="memory",
        detail={"owner_agent_id": "owner-agent"},
    )
    row = post.call_args.args[0]
    assert row["agent_id"] == agent
    assert row["detail"]["user_id"] == user
    assert row["detail"]["credential_id"] == "cred-1"
    assert (
        row["detail"]["credential_kind"] == request.headers["x-caura-credential-kind"]
    )
    assert row["detail"]["claimed_agent_id"] == "claimed-agent"
    assert row["detail"]["owner_agent_id"] == "owner-agent"


async def test_verification_window_is_bounded(api):
    app, _ctx, sc = api
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/audit-log/verify", params={"tenant_id": "t1", "limit": 50_001}
        )
    assert response.status_code == 422
    sc.verify_audit_chain.assert_not_awaited()


async def test_verification_is_single_flight_per_tenant(api):
    app, _ctx, sc = api
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        if sc.verify_audit_chain.await_count == 1:
            await release.wait()
        return {"tenant_id": "t1", "valid": True, "verified_count": 0}

    sc.verify_audit_chain.side_effect = blocked
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = asyncio.create_task(
            client.get("/audit-log/verify", params={"tenant_id": "t1"})
        )
        await asyncio.wait_for(entered.wait(), 2)
        try:
            second = await client.get("/audit-log/verify", params={"tenant_id": "t1"})
            assert second.status_code == 409
            sc.verify_audit_chain.assert_awaited_once()
        finally:
            release.set()
            await first


async def test_verification_is_rate_limited_per_tenant(api):
    app, _ctx, sc = api
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.get("/audit-log/verify", params={"tenant_id": "t1"})
        second = await client.get("/audit-log/verify", params={"tenant_id": "t1"})
    assert first.status_code == 200
    assert second.status_code == 429
    assert int(second.headers["Retry-After"]) > 0
    sc.verify_audit_chain.assert_awaited_once()


async def test_mcp_audit_records_verified_credential_and_restores_context(monkeypatch):
    from core_api import mcp_server
    from core_api.audit_actor import audit_principal

    monkeypatch.setattr(auth_mod.settings, "gateway_shared_secret", "gw")
    monkeypatch.setattr(
        mcp_server, "_refuse_if_suppressed", AsyncMock(return_value=False)
    )
    post = AsyncMock()
    monkeypatch.setattr(audit_service, "get_audit_queue", lambda: None)
    monkeypatch.setattr(audit_service, "_post_audit_sync", post)

    async def app(scope, receive, send):
        await audit_service.log_action(
            tenant_id="t1",
            agent_id="named-agent",
            action="create",
            resource_type="memory",
        )

    before = audit_principal.get()
    headers = {
        "x-tenant-id": "t1",
        "x-gateway-secret": "gw",
        "x-caura-credential-id": "install-1",
        "x-caura-credential-kind": "install_credential",
    }
    await mcp_server.MCPAuthMiddleware(app)(
        {
            "type": "http",
            "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
        },
        AsyncMock(),
        AsyncMock(),
    )
    row = post.call_args.args[0]
    assert row["agent_id"] is None
    assert row["detail"]["claimed_agent_id"] == "named-agent"
    assert row["detail"]["credential_id"] == "install-1"
    assert row["detail"]["credential_kind"] == "install_credential"
    assert row["detail"]["surface"] == "mcp"
    assert audit_principal.get() == before


async def test_distributed_verification_fails_closed_when_redis_unavailable(
    api, monkeypatch
):
    app, _ctx, sc = api
    monkeypatch.setattr(audit.settings, "redis_url", "redis://unused")
    monkeypatch.setattr(audit, "_get_redis", AsyncMock(return_value=None))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/audit-log/verify", params={"tenant_id": "t1"})
    assert response.status_code == 503
    sc.verify_audit_chain.assert_not_awaited()
    assert "t1" not in audit._verification_inflight


async def test_distributed_verification_releases_only_its_lease(api, monkeypatch):
    app, _ctx, sc = api
    redis = SimpleNamespace(set=AsyncMock(side_effect=[True, True]))
    release = AsyncMock()
    monkeypatch.setattr(audit.settings, "redis_url", "redis://unused")
    monkeypatch.setattr(audit, "_get_redis", AsyncMock(return_value=redis))
    monkeypatch.setattr(audit, "cache_delete_if", release)
    sc.verify_audit_chain.side_effect = RuntimeError("storage unavailable")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.get("/audit-log/verify", params={"tenant_id": "t1"})
    assert response.status_code == 500
    lease_call = redis.set.await_args_list[0]
    release.assert_awaited_once_with(*lease_call.args)
    assert "t1" not in audit._verification_inflight


@pytest.mark.parametrize("admission,status", [([False], 409), ([True, False], 429)])
async def test_another_replica_lease_or_budget_refuses_verification(
    api, monkeypatch, admission, status
):
    app, _ctx, sc = api
    redis = SimpleNamespace(set=AsyncMock(side_effect=admission))
    monkeypatch.setattr(audit.settings, "redis_url", "redis://unused")
    monkeypatch.setattr(audit, "_get_redis", AsyncMock(return_value=redis))
    release = AsyncMock()
    monkeypatch.setattr(audit, "cache_delete_if", release)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/audit-log/verify", params={"tenant_id": "t1"})
    assert response.status_code == status
    sc.verify_audit_chain.assert_not_awaited()
    assert release.await_count == int(admission[0])


async def test_memory_update_keeps_owner_separate_from_person(monkeypatch):
    from uuid import uuid4

    from core_api.audit_actor import audit_principal
    from core_api.schemas import MemoryUpdate
    from core_api.services import memory_service

    mid = uuid4()
    mem = {
        "id": str(mid),
        "agent_id": "owner",
        "content": "existing content",
        "weight": 0.5,
    }
    sc = SimpleNamespace(
        get_memory=AsyncMock(return_value=mem),
        update_memory=AsyncMock(return_value=mem),
        get_entity_links_for_memories=AsyncMock(return_value={}),
    )
    monkeypatch.setattr(memory_service, "get_storage_client", lambda: sc)
    monkeypatch.setattr(memory_service, "_dict_to_memory_out", lambda *a, **k: mem)
    monkeypatch.setattr(
        memory_service,
        "get_hooks",
        lambda: SimpleNamespace(audit_log=audit_service.log_action),
    )
    post = AsyncMock()
    monkeypatch.setattr(audit_service, "get_audit_queue", lambda: None)
    monkeypatch.setattr(audit_service, "_post_audit_sync", post)
    token = audit_principal.set(
        {
            "agent_id": None,
            "user_id": "person",
            "surface": "prism",
            "credential_id": "session-1",
            "credential_kind": "session",
        }
    )
    try:
        await memory_service.update_memory(mid, "t1", MemoryUpdate(weight=0.7))
    finally:
        audit_principal.reset(token)
    row = post.call_args.args[0]
    assert row["agent_id"] is None
    assert row["detail"]["owner_agent_id"] == "owner"
    assert row["detail"]["user_id"] == "person"
    assert row["detail"].get("claimed_agent_id") is None


@pytest.mark.parametrize("error", [TimeoutError, asyncio.CancelledError])
async def test_interrupted_verification_keeps_lease_until_expiry(
    api, monkeypatch, error
):
    from fastapi import HTTPException

    _app, ctx, sc = api
    redis = SimpleNamespace(set=AsyncMock(side_effect=[True, True]))
    release = AsyncMock()
    monkeypatch.setattr(audit.settings, "redis_url", "redis://unused")
    monkeypatch.setattr(audit, "_get_redis", AsyncMock(return_value=redis))
    monkeypatch.setattr(audit, "cache_delete_if", release)
    sc.verify_audit_chain.side_effect = error()
    with pytest.raises(
        HTTPException if error is TimeoutError else asyncio.CancelledError
    ):
        await audit.verify_audit_log(
            tenant_id="t1", limit=50_000, start_seq=1, auth=ctx
        )
    release.assert_not_awaited()
    assert "t1" not in audit._verification_inflight
    assert audit._verification_next_allowed["t1"] > audit.time.monotonic() + 170


@pytest.mark.parametrize("flagged", [True, False])
@pytest.mark.parametrize("check_child", [True, False])
async def test_content_edit_audit_preserves_safe_details(
    monkeypatch, flagged, check_child
):
    from uuid import uuid4

    from core_api.schemas import MemoryUpdate
    from core_api.services import memory_service, organization_settings, pii_update_gate
    from tests.conftest import close_scheduled_coro

    mid = uuid4()
    old, new = "previous private content", "replacement private content"
    mem = {"id": str(mid), "agent_id": "owner", "content": old, "metadata_": {}}
    sc = SimpleNamespace(
        get_memory=AsyncMock(return_value=mem),
        update_memory=AsyncMock(
            return_value={
                "derived_deleted": [{"id": str(uuid4()), "agent_id": "child-owner"}]
            }
        ),
        find_duplicate_hash=AsyncMock(return_value=None),
        reset_entity_artifacts=AsyncMock(),
        get_entity_links_for_memories=AsyncMock(return_value={}),
    )
    config = SimpleNamespace(
        enrichment_enabled=False,
        semantic_dedup_enabled=False,
        entity_extraction_enabled=False,
        governance_pii=None,
    )
    flags = {"contains_pii": True} if flagged else {}
    monkeypatch.setattr(
        organization_settings, "resolve_config", AsyncMock(return_value=config)
    )
    monkeypatch.setattr(
        pii_update_gate,
        "apply_pii_policy_to_update",
        AsyncMock(return_value=(new, flags)),
    )
    monkeypatch.setattr(memory_service, "get_storage_client", lambda: sc)
    monkeypatch.setattr(memory_service, "_dict_to_memory_out", lambda *a, **k: mem)
    monkeypatch.setattr(memory_service, "get_embedding", AsyncMock(return_value=[0.1]))
    monkeypatch.setattr(memory_service, "_revert_unlinked_losers", AsyncMock())
    monkeypatch.setattr(memory_service, "track_task", close_scheduled_coro)
    log = AsyncMock()
    monkeypatch.setattr(
        memory_service, "get_hooks", lambda: SimpleNamespace(audit_log=log)
    )
    await memory_service.update_memory(mid, "t1", MemoryUpdate(content=new))
    calls = [call.kwargs for call in log.await_args_list]
    parent = next(call for call in calls if call["action"] == "update")
    child = next(call for call in calls if call["action"] == "soft_delete")
    if check_child:
        assert child["agent_id"] is None
        assert child.get("detail", {}).get("owner_agent_id") == "child-owner"
        return
    detail = parent["detail"]
    assert detail["content_changed"] is True
    assert "content" not in detail["changes"]
    assert "content_sha256" not in detail["changes"]
    assert "content_sha256" not in detail
    assert hashlib.sha256(old.encode()).hexdigest() not in str(detail)


@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize(
    "identity,expected",
    [
        ("admin", "admin_key"),
        ("person", "person"),
        ("person-no-secret", "person"),
        ("mixed-kind", "agent_key"),
        ("tenant", "tenant_key"),
    ],
)
async def test_rest_and_mcp_record_the_same_normalized_credential_kind(
    monkeypatch, transport, identity, expected
):
    from core_api import mcp_server
    from core_api.audit_actor import audit_principal
    from tests._legacy_contracts import LEGACY_API_KEY_FIELD
    from tests.test_audit_actor import _resolve

    headers = {"x-tenant-id": "t1", "x-gateway-secret": "gw"}
    admin_key = "test-admin" if identity == "admin" else None
    gateway_secret = None if identity == "person-no-secret" else "gw"
    if identity == "admin":
        headers.update({"x-api-key": admin_key, "x-caura-credential-kind": "agent_key"})
    elif identity in {"person", "person-no-secret"}:
        headers.update({"x-org-role": "admin", "x-user-id": "user-1"})
    elif identity == "mixed-kind":
        headers.update(
            {"x-agent-id": "agent-1", "x-caura-credential-kind": "  AgEnT_KeY  "}
        )
    token = audit_principal.set(None)
    try:
        if transport == "rest":
            await _resolve(
                monkeypatch, headers, secret=gateway_secret, admin_key=admin_key
            )
            observed = audit_principal.get()["credential_kind"]
        else:
            monkeypatch.setattr(
                auth_mod.settings, "gateway_shared_secret", gateway_secret
            )
            monkeypatch.setattr(auth_mod.settings, LEGACY_API_KEY_FIELD, None)
            monkeypatch.setattr(auth_mod.settings, "is_standalone", False)
            monkeypatch.setattr(mcp_server, "get_admin_key", lambda: admin_key)
            monkeypatch.setattr(
                mcp_server, "_refuse_if_suppressed", AsyncMock(return_value=False)
            )
            observed_rows = []

            async def app(scope, receive, send):
                observed_rows.append(audit_principal.get()["credential_kind"])

            await mcp_server.MCPAuthMiddleware(app)(
                {
                    "type": "http",
                    "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
                },
                AsyncMock(),
                AsyncMock(),
            )
            observed = observed_rows[0]
        assert observed == expected
    finally:
        audit_principal.reset(token)
