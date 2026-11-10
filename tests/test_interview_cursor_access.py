"""Credential-owned cursor reads and compatible, scoped document surfaces."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import NAMESPACE_URL, uuid5

import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import JSONResponse

from core_api import mcp_server
from core_api.auth import CAURA_KEY_CREDENTIAL, AuthContext, get_auth_context
from core_api.config import settings
from core_api.routes import documents, interview
from core_api.services import interview_service
from core_api.services.interview_service import watermark_doc_id
from tests._mcp_test_helpers import parse_envelope

pytestmark = pytest.mark.unit
TENANT = "cursor-tenant"
OWNER = "cursor-owner"
NODE = "cc:machine:session"
COLLECTION = "interview_watermarks"


def row(node=NODE, owner=OWNER, collection=COLLECTION):
    data = {"node_id": node, "last_seq": 17, "private_marker": "other-stream-marker"}
    if owner is not None:
        data["agent_id"] = owner
    return {
        "id": str(uuid5(NAMESPACE_URL, node)),
        "tenant_id": TENANT,
        "collection": collection,
        "doc_id": watermark_doc_id(node),
        "fleet_id": None,
        "data": data,
        "created_at": datetime(2026, 11, 9, tzinfo=UTC),
        "updated_at": datetime(2026, 11, 9, tzinfo=UTC),
        "similarity": 0.9,
    }


def principal(kind):
    if kind == "shared":
        # The configured deployment key is explicitly tenant-admin authority,
        # even when its caller asserts an unrelated agent label.
        return AuthContext(
            TENANT, credential_id=CAURA_KEY_CREDENTIAL, agent_id="asserted"
        )
    if kind == "org-admin":
        return AuthContext(
            TENANT, org_role="admin", is_person=True, agent_id_verified=True
        )
    if kind == "system-admin":
        return AuthContext(None, is_admin=True)
    if kind == "install":
        return AuthContext(
            TENANT, is_install_credential=True, credential_kind="install_credential"
        )
    if kind == "member":
        return AuthContext(
            TENANT, org_role="member", is_person=True, agent_id_verified=True
        )
    if kind == "unverified":
        return AuthContext(TENANT, agent_id=OWNER, credential_kind="agent")
    return AuthContext(
        TENANT,
        agent_id=OWNER if kind == "owner" else "other-agent",
        agent_id_verified=True,
        credential_kind="agent",
    )


@pytest.fixture
def store(monkeypatch):
    sc = MagicMock()
    sc.rows = [row("foreign", "other-agent"), row(), row("legacy", None)]

    def get(*args, **kwargs):
        doc_id = kwargs.get("doc_id", args[2] if len(args) > 2 else None)
        return next((deepcopy(d) for d in sc.rows if d["doc_id"] == doc_id), None)

    sc.get_document = AsyncMock(side_effect=get)
    sc.query_documents = AsyncMock(side_effect=lambda *a, **k: deepcopy(sc.rows))
    sc.list_documents = AsyncMock(side_effect=lambda *a, **k: deepcopy(sc.rows))
    sc.search_documents_vector = AsyncMock(
        side_effect=lambda *a, **k: deepcopy(sc.rows)
    )
    sc.list_document_collections = AsyncMock(
        return_value={
            "collections": [
                {"name": "interview_jobs", "count": 25},
                {"name": COLLECTION, "count": 3},
                {"name": "notes", "count": 2},
            ],
            "count": 3,
        }
    )
    sc.upsert_document = AsyncMock()
    sc.get_agent = AsyncMock(return_value={"agent_id": OWNER, "trust_level": 3})
    for module in (documents, interview, interview_service, mcp_server):
        monkeypatch.setattr(module, "get_storage_client", lambda: sc)
    monkeypatch.setattr(documents, "check_and_increment", AsyncMock())
    monkeypatch.setattr(documents, "resolve_config", AsyncMock(return_value=None))
    monkeypatch.setattr(documents, "get_embedding", AsyncMock(return_value=[0.1] * 8))
    monkeypatch.setattr(mcp_server, "check_and_increment", AsyncMock())
    monkeypatch.setattr(mcp_server, "resolve_config", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "common.embedding.get_embedding", AsyncMock(return_value=[0.1] * 8)
    )
    return sc


@pytest.fixture
def rest(store):
    app = FastAPI()
    app.include_router(interview.router, prefix="/api/v1")
    app.include_router(documents.router, prefix="/api/v1")
    state = {"auth": principal("owner")}
    app.dependency_overrides[get_auth_context] = lambda: state["auth"]
    return app, state


async def rest_call(rest, method, path, **kwargs):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rest[0]), base_url="http://test"
    ) as c:
        return await c.request(method, "/api/v1" + path, **kwargs)


@pytest.mark.parametrize("kind", ["owner", "shared", "org-admin", "system-admin"])
async def test_owned_cursor_is_primary_read_and_returns_only_sequence(
    rest, store, kind
):
    rest[1]["auth"] = principal(kind)
    body = {"node_id": NODE}
    if kind == "system-admin":
        body["tenant_id"] = TENANT
    resp = await rest_call(rest, "POST", "/interview/cursor", json=body)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"last_seq": 17}
    call = store.get_document.await_args
    assert call.kwargs.get("read") is False
    assert call.kwargs.get("tenant_id", call.args[0] if call.args else None) == TENANT
    store.upsert_document.assert_not_awaited()


@pytest.mark.parametrize("kind", ["other", "unverified", "member", "install"])
async def test_cursor_rejects_non_owners_without_exposing_document(rest, store, kind):
    rest[1]["auth"] = principal(kind)
    resp = await rest_call(rest, "POST", "/interview/cursor", json={"node_id": NODE})
    assert resp.status_code == 403, resp.text
    assert "other-stream-marker" not in resp.text
    store.upsert_document.assert_not_awaited()


async def test_body_agent_id_cannot_supply_cursor_ownership(rest):
    rest[1]["auth"] = principal("other")
    resp = await rest_call(
        rest, "POST", "/interview/cursor", json={"node_id": NODE, "agent_id": OWNER}
    )
    assert resp.status_code in (403, 422), resp.text


@pytest.mark.parametrize("owner", [None, "other-agent"])
async def test_legacy_or_other_owner_is_not_an_empty_stream(rest, store, owner):
    store.rows = [row(owner=owner)]
    resp = await rest_call(rest, "POST", "/interview/cursor", json={"node_id": NODE})
    assert resp.status_code == 403, resp.text
    assert "last_seq" not in resp.json()


@pytest.mark.parametrize("kind", ["shared", "org-admin", "system-admin"])
async def test_privileged_cursor_can_read_ownerless_legacy_stream(rest, store, kind):
    store.rows = [row(owner=None)]
    rest[1]["auth"] = principal(kind)
    resp = await rest_call(
        rest, "POST", "/interview/cursor", json={"tenant_id": TENANT, "node_id": NODE}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"last_seq": 17}


async def test_minus_one_only_for_authorized_absent_stream(rest, store):
    store.rows = []
    resp = await rest_call(rest, "POST", "/interview/cursor", json={"node_id": NODE})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"last_seq": -1}
    store.upsert_document.assert_not_awaited()


@pytest.mark.parametrize("kind", ["owner", "org-admin", "shared"])
async def test_body_tenant_must_match_credential_even_when_read_scope_is_wider(
    rest, kind
):
    auth = principal(kind)
    auth.readable_tenant_ids = [TENANT, "other-tenant"]
    rest[1]["auth"] = auth
    resp = await rest_call(
        rest,
        "POST",
        "/interview/cursor",
        json={"tenant_id": "other-tenant", "node_id": NODE},
    )
    assert resp.status_code == 403, resp.text


async def test_matching_optional_body_tenant_is_allowed(rest):
    resp = await rest_call(
        rest, "POST", "/interview/cursor", json={"tenant_id": TENANT, "node_id": NODE}
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize("surface", ["get", "list", "query", "search"])
async def test_legacy_rest_reads_scope_agent_to_its_own_watermarks(rest, surface):
    doc_id = watermark_doc_id(NODE)
    if surface == "get":
        resp = await rest_call(
            rest,
            "GET",
            "/documents/" + doc_id,
            params={"tenant_id": TENANT, "collection": COLLECTION},
        )
        rows = [resp.json()]
    elif surface == "list":
        resp = await rest_call(
            rest,
            "GET",
            "/documents",
            params={"tenant_id": TENANT, "collection": COLLECTION},
        )
        rows = resp.json()
    else:
        body = {"tenant_id": TENANT, "collection": COLLECTION}
        if surface == "search":
            body["query"] = "progress"
        resp = await rest_call(rest, "POST", "/documents/" + surface, json=body)
        rows = resp.json().get("items", []) if surface == "search" else resp.json()
    assert resp.status_code == 200, resp.text
    assert [r["doc_id"] for r in rows] == [doc_id]


@pytest.mark.parametrize("kind", ["owner", "unverified", "member", "install"])
async def test_legacy_get_cannot_read_another_stream(rest, kind):
    rest[1]["auth"] = principal(kind)
    resp = await rest_call(
        rest,
        "GET",
        "/documents/" + watermark_doc_id("foreign"),
        params={"tenant_id": TENANT, "collection": COLLECTION},
    )
    assert resp.status_code == 403, resp.text
    assert "other-stream-marker" not in resp.text


@pytest.mark.parametrize("surface", ["get", "list", "query", "search"])
async def test_shared_deployment_key_keeps_legacy_cursor_reads(rest, surface):
    rest[1]["auth"] = principal("shared")
    if surface in ("get", "list"):
        path = (
            "/documents/" + watermark_doc_id("legacy")
            if surface == "get"
            else "/documents"
        )
        resp = await rest_call(
            rest, "GET", path, params={"tenant_id": TENANT, "collection": COLLECTION}
        )
    else:
        body = {"tenant_id": TENANT, "collection": COLLECTION}
        if surface == "search":
            body["query"] = "progress"
        resp = await rest_call(rest, "POST", "/documents/" + surface, json=body)
    assert resp.status_code == 200, resp.text


async def test_rest_collection_listing_hides_internal_names_and_recounts(rest):
    resp = await rest_call(
        rest, "GET", "/documents/collections", params={"tenant_id": TENANT}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"collections": [{"name": "notes", "count": 2}], "count": 1}


@pytest.fixture
def mcp_call(store, monkeypatch):
    monkeypatch.setattr(settings, "is_standalone", False)
    monkeypatch.setattr(settings, "admin_api_key", "cursor-admin-fixture")
    monkeypatch.setattr(settings, "gateway_shared_secret", "cursor-gateway-fixture")
    monkeypatch.setattr(
        mcp_server, "_refuse_if_suppressed", AsyncMock(return_value=False)
    )

    async def call(kind, **kwargs):
        monkeypatch.setattr(
            settings,
            "memclaw_api_key",  # legacy-name-ok: exercise the existing dual-read config alias
            "cursor-shared-fixture" if kind == "shared" else None,
        )
        headers = {"X-Tenant-ID": TENANT, "X-Gateway-Secret": "cursor-gateway-fixture"}
        if kind == "system-admin":
            headers["X-API-Key"] = "cursor-admin-fixture"
        elif kind == "shared":
            headers.update(
                {"X-API-Key": "cursor-shared-fixture", "X-Agent-ID": "asserted"}
            )
        elif kind == "org-admin":
            headers["X-Org-Role"] = "admin"
        elif kind == "install":
            headers["X-Caura-Credential-Kind"] = "install_credential"
        elif kind == "member":
            headers["X-Org-Role"] = "member"
        else:
            headers.update(
                {
                    "X-Agent-ID": OWNER if kind == "owner" else "other-agent",
                    "X-Caura-Credential-Kind": "agent",
                }
            )

        async def app(scope, receive, send):
            args = await Request(scope, receive).json()
            out = await mcp_server.caura_doc(agent_id=OWNER, **args)
            await JSONResponse(parse_envelope(out))(scope, receive, send)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=mcp_server.MCPAuthMiddleware(app)),
            base_url="http://test",
        ) as c:
            response = await c.post("/mcp", json=kwargs, headers=headers)
        assert response.status_code == 200, response.text
        return response.json()

    return call


@pytest.mark.parametrize("op", ["read", "query"])
@pytest.mark.parametrize("kind", ["other", "member", "install"])
async def test_mcp_generic_reads_cannot_use_body_identity_as_ownership(
    mcp_call, store, op, kind
):
    # The target belongs to OWNER; a caller's separate, owned stream is not
    # part of this refusal test (and is explicitly covered below).
    store.rows = [row()]
    out = await mcp_call(
        kind, op=op, collection=COLLECTION, doc_id=watermark_doc_id(NODE)
    )
    if op == "read":
        assert "error" in out, out
    else:
        assert "error" in out or not out.get("items"), out
    assert "other-stream-marker" not in str(out)


@pytest.mark.parametrize("kind", ["owner", "shared", "org-admin", "system-admin"])
async def test_mcp_compatible_reads_allow_owner_and_explicit_privileged_paths(
    mcp_call, kind
):
    out = await mcp_call(
        kind, op="read", collection=COLLECTION, doc_id=watermark_doc_id(NODE)
    )
    assert "error" not in out, out
    assert out["data"]["last_seq"] == 17


async def test_mcp_owner_query_filters_foreign_and_legacy_rows(mcp_call):
    out = await mcp_call("owner", op="query", collection=COLLECTION)
    assert "error" not in out, out
    assert [r["doc_id"] for r in out["items"]] == [watermark_doc_id(NODE)]
    assert out["count"] == 1


async def test_mcp_collection_listing_hides_internal_names_and_recounts(mcp_call):
    out = await mcp_call("owner", op="list_collections")
    assert out == {"collections": [{"name": "notes", "count": 2}], "count": 1}


async def test_cursor_remains_read_only_for_read_capability_credentials(rest, store):
    auth = principal("owner")
    auth.is_read_only = True
    auth.capabilities = {"read"}
    rest[1]["auth"] = auth
    resp = await rest_call(rest, "POST", "/interview/cursor", json={"node_id": NODE})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"last_seq": 17}
    store.upsert_document.assert_not_awaited()


@pytest.mark.parametrize("kind", ["unverified", "member", "install"])
@pytest.mark.parametrize("surface", ["list", "query", "search"])
async def test_legacy_rest_bulk_reads_refuse_credentials_without_cursor_authority(
    rest, kind, surface
):
    rest[1]["auth"] = principal(kind)
    if surface == "list":
        resp = await rest_call(
            rest,
            "GET",
            "/documents",
            params={"tenant_id": TENANT, "collection": COLLECTION},
        )
    else:
        body = {"tenant_id": TENANT, "collection": COLLECTION}
        if surface == "search":
            body["query"] = "progress"
        resp = await rest_call(rest, "POST", "/documents/" + surface, json=body)
    assert resp.status_code == 403, resp.text
    assert "other-stream-marker" not in resp.text


async def test_unscoped_document_search_cannot_bypass_watermark_ownership(rest, store):
    note = row("public-note", None, "notes")
    store.rows.append(note)
    resp = await rest_call(
        rest,
        "POST",
        "/documents/search",
        json={"tenant_id": TENANT, "query": "progress"},
    )
    assert resp.status_code == 200, resp.text
    assert [d["doc_id"] for d in resp.json()["items"]] == [
        watermark_doc_id(NODE),
        note["doc_id"],
    ]


@pytest.mark.parametrize("kind", ["org-admin", "system-admin"])
async def test_legacy_admin_get_can_read_ownerless_watermark(rest, kind):
    rest[1]["auth"] = principal(kind)
    resp = await rest_call(
        rest,
        "GET",
        "/documents/" + watermark_doc_id("legacy"),
        params={"tenant_id": TENANT, "collection": COLLECTION},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["last_seq"] == 17


async def test_system_admin_must_select_a_tenant_when_credential_has_none(rest, store):
    rest[1]["auth"] = principal("system-admin")
    resp = await rest_call(rest, "POST", "/interview/cursor", json={"node_id": NODE})
    assert resp.status_code == 400, resp.text
    store.get_document.assert_not_awaited()


@pytest.mark.parametrize("collection", [COLLECTION, None])
async def test_mcp_search_has_no_alternate_watermark_read_bypass(
    mcp_call, store, collection
):
    note = row("public-note", None, "notes")
    if collection is None:
        store.rows.append(note)
    out = await mcp_call("owner", op="search", collection=collection, query="progress")
    assert "error" not in out, out
    expected = [watermark_doc_id(NODE)] + (
        [note["doc_id"]] if collection is None else []
    )
    assert [d["doc_id"] for d in out["items"]] == expected


async def test_caller_query_filter_cannot_widen_watermark_ownership(rest):
    resp = await rest_call(
        rest,
        "POST",
        "/documents/query",
        json={
            "tenant_id": TENANT,
            "collection": COLLECTION,
            "where": {"agent_id": "other-agent"},
        },
    )
    assert resp.status_code == 200, resp.text
    assert {d["doc_id"] for d in resp.json()} <= {watermark_doc_id(NODE)}


async def test_mcp_query_uses_verified_principal_even_with_another_agent_argument(
    mcp_call, store
):
    out = await mcp_call("other", op="query", collection=COLLECTION)
    assert "error" not in out, out
    assert [d["doc_id"] for d in out["items"]] == [watermark_doc_id("foreign")]
    assert store.query_documents.await_args.args[0]["where"] == {
        "agent_id": "other-agent"
    }


@pytest.mark.parametrize("last_seq", [None, True, -1, "17", 1.5])
async def test_existing_invalid_cursor_is_not_an_absent_stream(rest, store, last_seq):
    store.rows = [row()]
    store.rows[0]["data"]["last_seq"] = last_seq
    resp = await rest_call(rest, "POST", "/interview/cursor", json={"node_id": NODE})
    assert resp.status_code == 503, resp.text


async def test_cursor_storage_failure_cannot_reset_stream(rest, store):
    store.get_document.side_effect = httpx.ConnectError("synthetic storage failure")
    with pytest.raises(httpx.ConnectError):
        await rest_call(rest, "POST", "/interview/cursor", json={"node_id": NODE})


async def test_unverified_agent_label_cannot_inherit_admin_authority(rest):
    rest[1]["auth"] = AuthContext(TENANT, agent_id=OWNER, org_role="admin")
    resp = await rest_call(rest, "POST", "/interview/cursor", json={"node_id": NODE})
    assert resp.status_code == 403, resp.text


async def test_mcp_cursor_provenance_is_request_scoped(mcp_call):
    await mcp_call(
        "shared", op="read", collection=COLLECTION, doc_id=watermark_doc_id(NODE)
    )
    assert mcp_server._cursor_auth_var.get() is None
    out = await mcp_call(
        "member", op="read", collection=COLLECTION, doc_id=watermark_doc_id(NODE)
    )
    assert "error" in out, out


async def test_explicit_empty_tenant_is_not_an_omitted_tenant(rest, store):
    resp = await rest_call(
        rest, "POST", "/interview/cursor", json={"node_id": NODE, "tenant_id": ""}
    )
    assert resp.status_code == 400, resp.text
    store.get_document.assert_not_awaited()


@pytest.mark.parametrize("collection", [COLLECTION, None, "notes"])
async def test_empty_search_diagnostics_cannot_count_hidden_watermarks(
    rest, store, collection
):
    store.search_documents_vector = AsyncMock(return_value=[])
    store.count_unindexed_documents = AsyncMock(return_value=42)
    resp = await rest_call(
        rest,
        "POST",
        "/documents/search",
        json={"tenant_id": TENANT, "collection": collection, "query": "progress"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == []
    if collection == "notes":
        assert resp.json()["unindexed_count"] == 42
        store.count_unindexed_documents.assert_awaited_once()
    else:
        assert "unindexed_count" not in resp.json()
        assert "note" not in resp.json()
        store.count_unindexed_documents.assert_not_awaited()
