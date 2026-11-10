"""Approved artifact delivery is bound to a registered receiver, not a fleet claim."""

from __future__ import annotations

import hashlib
import importlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import NAMESPACE_URL, uuid5

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from core_api.auth import ADMIN_KEY_CREDENTIAL, AuthContext, get_auth_context
from core_api.clients import storage_client
from core_api.routes import documents
from core_api.services.interview_service import (
    refuse_system_collection,
    refuse_system_read,
)

pytestmark = pytest.mark.unit
TENANT = "delivery-tenant"
OWNER = "delivery-owner"
PERSON = "delivery-approver"
NODE = str(uuid5(NAMESPACE_URL, "delivery-node"))
PEER = str(uuid5(NAMESPACE_URL, "delivery-peer"))
SLUG = "agent/safe-delivery"
GRANTS = "_skill_delivery_grants"
NOW = datetime(2026, 11, 10, tzinfo=UTC)


def rendered(data):
    content = data["content"]
    if content.startswith("---\n"):
        return content
    return (
        "---\nname: "
        + json.dumps(data["name"], ensure_ascii=False)
        + "\ndescription: "
        + json.dumps(data["description"], ensure_ascii=False)
        + "\n---\n\n"
        + content
    )


def digest(content):
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def doc(doc_id=SLUG, collection="skills", **changes):
    data = {
        "name": "safe-delivery",
        "description": "Use for a reviewed delivery fixture.",
        "content": "# Reviewed skill\n",
        "status": "active",
    }
    data.update(changes)
    return {
        "id": str(uuid5(NAMESPACE_URL, collection + doc_id)),
        "tenant_id": TENANT,
        "collection": collection,
        "doc_id": doc_id,
        "fleet_id": "fleet-a",
        "data": data,
        "created_at": NOW,
        "updated_at": NOW,
    }


def principal(kind):
    if kind == "org-admin":
        return AuthContext(TENANT, user_id=PERSON, org_role="admin", is_person=True)
    if kind == "system-admin":
        return AuthContext(None, is_admin=True, credential_id=ADMIN_KEY_CREDENTIAL)
    if kind == "member":
        return AuthContext(TENANT, user_id="member", org_role="member", is_person=True)
    if kind == "install":
        return AuthContext(TENANT, is_install_credential=True, install_uuid="install-a")
    if kind == "unverified":
        return AuthContext(TENANT, agent_id=OWNER)
    return AuthContext(
        TENANT,
        agent_id=OWNER if kind == "owner" else "another-agent",
        agent_id_verified=True,
        credential_kind="agent",
    )


@pytest.fixture
def delivery(monkeypatch):
    sc = MagicMock()
    sc.rows = [doc()]
    sc.nodes = {
        NODE: {
            "id": NODE,
            "tenant_id": TENANT,
            "node_name": "node-a",
            "fleet_id": "fleet-a",
            "owner_principal": f"agent:{OWNER}",
        },
        PEER: {
            "id": PEER,
            "tenant_id": TENANT,
            "node_name": "node-b",
            "fleet_id": "fleet-a",
            "owner_principal": "agent:another-agent",
        },
    }

    def get(*args, **kwargs):
        tenant = kwargs.get("tenant_id", args[0] if args else TENANT)
        collection = kwargs.get("collection", args[1] if len(args) > 1 else "skills")
        key = kwargs.get("doc_id", args[2] if len(args) > 2 else "")
        return next(
            (
                deepcopy(r)
                for r in sc.rows
                if (r["tenant_id"], r["collection"], r["doc_id"])
                == (tenant, collection, key)
            ),
            None,
        )

    def query(params, **kwargs):
        rows = [
            r
            for r in sc.rows
            if r["tenant_id"] == params["tenant_id"]
            and r["collection"] == params["collection"]
        ]
        rows = [
            r
            for r in rows
            if all(
                r["data"].get(k) == v for k, v in (params.get("where") or {}).items()
            )
        ]
        offset = params.get("offset", 0)
        return deepcopy(rows[offset : offset + params.get("limit", 1000)])

    def put(payload):
        row = doc(payload["doc_id"], payload["collection"])
        row.update(deepcopy(payload))
        sc.rows[:] = [
            r
            for r in sc.rows
            if (r["collection"], r["doc_id"]) != (row["collection"], row["doc_id"])
        ]
        sc.rows.append(row)
        return deepcopy(row)

    def get_node_binding(*args, **kwargs):
        node_id = kwargs.get("node_id", args[1] if len(args) > 1 else "")
        return deepcopy(sc.nodes.get(str(node_id)))

    async def read_approvals(*, tenant_id, doc_ids, node_id, owner_principal):
        matching = []
        recipient = {"node_id": node_id, "owner_principal": owner_principal}
        for row in sc.rows:
            if (
                row["tenant_id"] == tenant_id
                and row["collection"] == GRANTS
                and row["doc_id"] in doc_ids
            ):
                data = row["data"]
                if recipient in data.get("recipients", []):
                    matching.append(
                        {
                            **deepcopy(row),
                            "data": {**deepcopy(data), "recipients": [recipient]},
                        }
                    )
        return matching

    sc.read_skill_delivery_records = AsyncMock(side_effect=read_approvals)
    sc.get_document = AsyncMock(side_effect=get)
    sc.query_documents = AsyncMock(side_effect=query)
    sc.upsert_document = AsyncMock(side_effect=put)
    sc.upsert_skill_delivery_record = AsyncMock(side_effect=put)
    sc.upsert_document_xmax = AsyncMock(side_effect=put)
    sc.get_node_binding = AsyncMock(side_effect=get_node_binding)
    sc.list_nodes = AsyncMock(
        side_effect=lambda *a, **k: deepcopy(list(sc.nodes.values()))
    )
    sc.get_agent = AsyncMock(
        return_value={"agent_id": OWNER, "trust_level": 3, "fleet_id": "fleet-a"}
    )
    sc.create_audit_log = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(storage_client, "_client", sc)
    monkeypatch.setattr(documents, "get_storage_client", lambda: sc)
    # Missing feature routes must produce HTTP failures on main, not import or
    # collection errors. Once implemented, exercise their real route handlers.
    app = FastAPI()
    for name in ("core_api.routes.skill_delivery",):
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name != name:
                raise
        else:
            if hasattr(module, "get_storage_client"):
                monkeypatch.setattr(module, "get_storage_client", lambda: sc)
            app.include_router(module.router, prefix="/api/v1")
    app.include_router(documents.router, prefix="/api/v1")
    state = {"auth": principal("owner")}
    app.dependency_overrides[get_auth_context] = lambda: state["auth"]
    return app, state, sc


async def call(delivery, method, path, **kwargs):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=delivery[0]), base_url="http://test"
    ) as client:
        return await client.request(method, "/api/v1" + path, **kwargs)


async def approve(delivery, nodes=None, **extra):
    delivery[1]["auth"] = principal("org-admin")
    return await call(
        delivery,
        "POST",
        f"/skills/{SLUG}/delivery",
        json={
            "tenant_id": TENANT,
            "expected_artifact_hash": digest(rendered(delivery[2].rows[0]["data"])),
            "node_ids": nodes if nodes is not None else [NODE],
            **extra,
        },
    )


async def manifest(delivery, **extra):
    return await call(
        delivery,
        "POST",
        "/skills/installable",
        json={
            "tenant_id": TENANT,
            "node_id": NODE,
            "delivery_version": 1,
            **extra,
        },
    )


def payload(response):
    assert response.status_code == 200, response.text
    result = response.json()
    assert isinstance(result, dict), (
        "delivery needs a complete versioned manifest, not an unbound catalog"
    )
    assert result["delivery_version"] == 1
    assert result["complete"] is True
    return result


@pytest.mark.parametrize("kind", ["owner", "org-admin", "system-admin"])
async def test_old_unversioned_requests_cannot_pull_new_skill_bytes(delivery, kind):
    delivery[1]["auth"] = principal(kind)
    response = await call(
        delivery,
        "POST",
        "/skills/installable",
        json={"tenant_id": TENANT, "fleet_id": "fleet-a"},
    )
    assert response.status_code in (403, 422), response.text
    assert "Reviewed skill" not in response.text


@pytest.mark.parametrize("kind", ["owner", "other", "member", "install", "unverified"])
async def test_only_admin_can_approve_a_delivery_snapshot(delivery, kind):
    delivery[1]["auth"] = principal(kind)
    response = await call(
        delivery,
        "POST",
        f"/skills/{SLUG}/delivery",
        json={
            "tenant_id": TENANT,
            "expected_artifact_hash": digest(rendered(doc()["data"])),
            "node_ids": [NODE],
        },
    )
    assert response.status_code == 403, response.text
    delivery[2].upsert_document.assert_not_awaited()
    delivery[2].upsert_skill_delivery_record.assert_not_awaited()


async def test_approval_binds_artifact_receiver_owner_and_real_approver(delivery):
    response = await approve(delivery)
    assert response.status_code == 200, response.text
    approval = response.json()
    assert approval["artifact_hash"] == digest(rendered(doc()["data"]))
    assert approval["grant_id"]
    assert {r["node_id"]: r["owner_principal"] for r in approval["recipients"]} == {
        NODE: f"agent:{OWNER}"
    }
    assert PERSON in str(approval["approved_by"])
    assert (
        datetime.fromisoformat(approval["approved_at"].replace("Z", "+00:00")).year
        >= 2026
    )
    assert delivery[2].rows[0]["data"]["status"] == "active"
    assert delivery[2].get_document.await_args.kwargs.get("read") is False


async def test_approval_does_not_accept_caller_supplied_attribution(delivery):
    response = await approve(
        delivery, approved_by="forged-approver", approved_at="2000-01-01T00:00:00Z"
    )
    if response.status_code == 422:
        delivery[2].upsert_document.assert_not_awaited()
        delivery[2].upsert_skill_delivery_record.assert_not_awaited()
        return
    assert response.status_code == 200, response.text
    assert PERSON in str(response.json()["approved_by"])
    assert "forged-approver" not in str(response.json()["approved_by"])
    assert not response.json()["approved_at"].startswith("2000-")


async def test_stale_artifact_hash_cannot_approve_a_different_version(delivery):
    response = await approve(delivery, expected_artifact_hash="sha256:" + "0" * 64)
    assert response.status_code == 409, response.text
    delivery[2].upsert_document.assert_not_awaited()
    delivery[2].upsert_skill_delivery_record.assert_not_awaited()


@pytest.mark.parametrize("kind", ["foreign", "missing", "unbound"])
async def test_approval_rejects_unusable_receiver_bindings(delivery, kind):
    sc = delivery[2]
    if kind == "foreign":
        sc.nodes[NODE]["tenant_id"] = "another-tenant"
    elif kind == "missing":
        sc.nodes.pop(NODE)
    else:
        sc.nodes[NODE]["owner_principal"] = None
    response = await approve(delivery)
    assert response.status_code in (403, 404, 409, 422), response.text
    sc.upsert_document.assert_not_awaited()
    sc.upsert_skill_delivery_record.assert_not_awaited()


async def test_legacy_active_skill_is_held_without_shipping_its_body(delivery):
    before = deepcopy(delivery[2].rows)
    result = payload(await manifest(delivery))
    assert result["documents"] == []
    assert any(
        h["doc_id"] == SLUG and h["reason"] == "approval_required"
        for h in result["held"]
    )
    assert "Reviewed skill" not in json.dumps(result)
    assert delivery[2].rows == before
    delivery[2].upsert_document.assert_not_awaited()
    delivery[2].upsert_skill_delivery_record.assert_not_awaited()


async def test_approved_manifest_contains_hash_bound_rendered_artifact(delivery):
    approval = await approve(delivery)
    assert approval.status_code == 200, approval.text
    delivery[1]["auth"] = principal("owner")
    result = payload(await manifest(delivery))
    assert [r["doc_id"] for r in result["documents"]] == [SLUG]
    row = result["documents"][0]
    assert row["delivery"]["artifact_hash"] == digest(rendered(row["data"]))
    assert row["delivery"]["grant_id"] == approval.json()["grant_id"]
    assert row["delivery"]["bytes"] == len(rendered(row["data"]).encode("utf-8"))
    assert (
        len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= 4 * 1024 * 1024
    )
    assert delivery[2].query_documents.await_args.kwargs.get("read") is False


@pytest.mark.parametrize("mutation", ["content", "description", "name", "owner"])
async def test_changed_content_or_receiver_cannot_inherit_a_grant(delivery, mutation):
    response = await approve(delivery)
    assert response.status_code == 200, response.text
    if mutation == "owner":
        delivery[2].nodes[NODE]["owner_principal"] = "agent:replacement"
        delivery[1]["auth"] = AuthContext(
            TENANT, agent_id="replacement", agent_id_verified=True
        )
    else:
        delivery[2].rows[0]["data"][mutation] += " changed"
        delivery[1]["auth"] = principal("owner")
    response = await manifest(delivery)
    if response.status_code == 200:
        assert payload(response)["documents"] == []
    else:
        assert response.status_code in (403, 409), response.text


async def test_new_node_in_the_same_fleet_does_not_inherit_the_snapshot(delivery):
    response = await approve(delivery)
    assert response.status_code == 200, response.text
    delivery[1]["auth"] = principal("other")
    assert (
        payload(await manifest(delivery, node_id=PEER, fleet_id="fleet-a"))["documents"]
        == []
    )


@pytest.mark.parametrize("kind", ["other", "member", "unverified"])
async def test_fleet_and_agent_claims_do_not_prove_receiver_ownership(delivery, kind):
    delivery[1]["auth"] = principal(kind)
    response = await manifest(delivery, fleet_id="fleet-a", agent_id=OWNER)
    assert response.status_code == 403, response.text
    assert "Reviewed skill" not in response.text


async def test_body_tenant_cannot_widen_the_delivery_scope(delivery):
    response = await manifest(delivery, tenant_id="another-tenant")
    assert response.status_code == 403, response.text
    delivery[2].query_documents.assert_not_awaited()


@pytest.mark.parametrize("field", ["content", "description", "name"])
async def test_final_rendered_bytes_include_frontmatter_in_the_delivery_cap(
    delivery, field
):
    delivery[2].rows[0]["data"][field] = "界" * 22000
    response = await approve(delivery)
    assert response.status_code in (413, 422), response.text
    delivery[2].upsert_document.assert_not_awaited()
    delivery[2].upsert_skill_delivery_record.assert_not_awaited()


async def test_exact_rendered_byte_limit_can_be_approved(delivery):
    data = delivery[2].rows[0]["data"]
    overhead = len(rendered({**data, "content": ""}).encode("utf-8"))
    data["content"] = "x" * (65536 - overhead)
    response = await approve(delivery)
    assert response.status_code == 200, response.text
    assert response.json()["artifact_hash"] == digest(rendered(data))


async def test_manifest_limit_is_not_a_partial_success(delivery):
    delivery[2].rows = [doc(f"agent/legacy-{n}") for n in range(1001)]
    response = await manifest(delivery)
    if response.status_code == 200:
        result = payload(response)
        assert len(result["documents"]) + len(result["held"]) == 1001, (
            "a truncated catalog must not become a prune instruction"
        )
    else:
        assert response.status_code in (413, 422), response.text


@pytest.mark.parametrize("guard", [refuse_system_collection, refuse_system_read])
def test_rest_and_mcp_shared_guards_protect_delivery_grants(guard):
    # REST and MCP write/delete/read handlers share these guards. Even an
    # administrator uses the approval API rather than generic grant documents.
    with pytest.raises(HTTPException) as error:
        guard(GRANTS)
    assert error.value.status_code in (400, 403)


async def test_grant_records_are_not_readable_through_generic_documents(delivery):
    delivery[2].rows.append(doc("protected-grant", GRANTS))
    response = await call(
        delivery,
        "GET",
        "/documents/protected-grant",
        params={"tenant_id": TENANT, "collection": GRANTS},
    )
    assert response.status_code in (400, 403, 404), response.text
    assert "Reviewed skill" not in response.text


async def test_ordinary_document_read_remains_available(delivery):
    delivery[2].rows.append(doc("ordinary", "notes"))
    response = await call(
        delivery,
        "GET",
        "/documents/ordinary",
        params={"tenant_id": TENANT, "collection": "notes"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["content"] == "# Reviewed skill\n"


@pytest.mark.parametrize("method", ["POST", "DELETE"])
async def test_even_admin_generic_writes_cannot_forge_or_remove_grants(
    delivery, method
):
    delivery[1]["auth"] = principal("org-admin")
    if method == "POST":
        response = await call(
            delivery,
            method,
            "/documents",
            json={
                "tenant_id": TENANT,
                "collection": GRANTS,
                "doc_id": "forged",
                "data": {"approved_by": PERSON, "recipients": [{"node_id": NODE}]},
            },
        )
    else:
        response = await call(
            delivery,
            method,
            "/documents/forged",
            params={"tenant_id": TENANT, "collection": GRANTS},
        )
    assert response.status_code in (400, 403), response.text
    delivery[2].upsert_document.assert_not_awaited()
    delivery[2].upsert_skill_delivery_record.assert_not_awaited()
    delivery[2].delete_document.assert_not_called()


async def test_saturated_storage_page_is_never_a_complete_manifest(delivery):
    delivery[2].rows = [doc(f"agent/legacy-{n}") for n in range(5000)]
    response = await manifest(delivery)
    assert response.status_code == 413, response.text


async def test_complete_manifest_over_wire_budget_fails_in_full(delivery):
    sc = delivery[2]
    sc.rows = [doc(f"agent/large-{n}", content="x" * 64000) for n in range(70)]
    delivery[1]["auth"] = principal("org-admin")
    for row in list(sc.rows):
        response = await call(
            delivery,
            "POST",
            f"/skills/{row['doc_id']}/delivery",
            json={
                "tenant_id": TENANT,
                "node_ids": [NODE],
                "expected_artifact_hash": digest(rendered(row["data"])),
            },
        )
        assert response.status_code == 200, response.text
    delivery[1]["auth"] = principal("owner")
    response = await manifest(delivery)
    assert response.status_code == 413, "must not return an approved prefix"
    assert "x" * 100 not in response.text


async def test_shared_tenant_receiver_matches_only_a_tenant_owned_node(delivery):
    from core_api.auth import CAURA_KEY_CREDENTIAL

    delivery[1]["auth"] = AuthContext(
        TENANT, credential_id=CAURA_KEY_CREDENTIAL, agent_id="asserted"
    )
    assert (await manifest(delivery)).status_code == 403
    delivery[2].nodes[NODE]["owner_principal"] = "tenant"
    result = payload(await manifest(delivery))
    assert result["documents"] == []


async def test_reapproval_replaces_targets_but_keeps_version_history(delivery):
    first = await approve(delivery)
    second = await approve(delivery, nodes=[PEER])
    assert first.status_code == second.status_code == 200
    assert first.json()["grant_id"] != second.json()["grant_id"]
    ids = {row["doc_id"] for row in delivery[2].rows if row["collection"] == GRANTS}
    assert {first.json()["grant_id"], second.json()["grant_id"]} <= ids
    delivery[1]["auth"] = principal("owner")
    assert payload(await manifest(delivery))["documents"] == []
    delivery[1]["auth"] = principal("other")
    assert len(payload(await manifest(delivery, node_id=PEER))["documents"]) == 1


async def test_primary_node_lookup_preserves_tenant_scope(monkeypatch):
    from types import SimpleNamespace
    from uuid import UUID

    from core_api.clients.storage_client import CoreStorageClient
    from core_storage_api.routers import fleet

    svc = MagicMock()
    svc.fleet_get_node_by_id = AsyncMock(
        return_value=SimpleNamespace(
            id=UUID(NODE),
            tenant_id=TENANT,
            owner_principal=f"agent:{OWNER}",
        )
    )
    monkeypatch.setattr(fleet, "_svc", svc)
    app = FastAPI()
    app.include_router(fleet.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = await client.post(
            "/fleet/nodes/get", json={"tenant_id": TENANT, "node_id": NODE}
        )
        assert result.status_code == 200, result.text
        assert result.json()["owner_principal"] == f"agent:{OWNER}"
        svc.fleet_get_node_by_id.assert_awaited_once_with(
            tenant_id=TENANT, node_id=UUID(NODE)
        )
        svc.fleet_get_node_by_id.return_value = None
        assert (
            await client.post(
                "/fleet/nodes/get", json={"tenant_id": "other", "node_id": NODE}
            )
        ).status_code == 404
        assert (
            await client.post(
                "/fleet/nodes/get", json={"tenant_id": TENANT, "node_id": "label"}
            )
        ).status_code == 422
    sc = CoreStorageClient(http=MagicMock())
    sc._post_optional = AsyncMock(return_value={"id": NODE})
    await sc.get_node_binding(TENANT, NODE)
    sc._post_optional.assert_awaited_once_with(
        "/fleet/nodes/get", {"tenant_id": TENANT, "node_id": NODE}, read=False
    )


@pytest.mark.parametrize("operation", ["read", "query", "write", "delete"])
async def test_mcp_generic_operations_cannot_access_approval_records(
    delivery, mcp_env, monkeypatch, operation
):
    from core_api import mcp_server
    from tests._mcp_test_helpers import parse_envelope

    monkeypatch.setattr(mcp_server, "get_storage_client", lambda: delivery[2])
    monkeypatch.setattr(
        mcp_server, "_refuse_if_suppressed", AsyncMock(return_value=False)
    )
    result = await mcp_server.caura_doc(
        op=operation,
        collection=GRANTS,
        doc_id="forged",
        data={"approved_by": PERSON},
        agent_id=OWNER,
    )
    envelope = parse_envelope(result)
    assert "error" in envelope, envelope
    assert "system-managed" in str(envelope), envelope
    delivery[2].upsert_document.assert_not_awaited()
    delivery[2].upsert_skill_delivery_record.assert_not_awaited()
    delivery[2].delete_document.assert_not_called()


async def test_generic_storage_cannot_preseed_reserved_approvals():
    from core_storage_api.routers import documents as stored_documents

    app = FastAPI()
    app.include_router(stored_documents.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = await client.post(
            "/documents",
            json={
                "tenant_id": TENANT,
                "collection": GRANTS,
                "doc_id": "preseed",
                "system": True,
                "force": True,
                "data": {"approved_by": PERSON},
            },
        )
    assert result.status_code == 400, result.text
    assert "system-managed" in result.text


async def test_internal_writer_has_a_fixed_reserved_destination(monkeypatch):
    from core_storage_api.routers import skill_delivery

    svc = MagicMock()
    svc.document_upsert = AsyncMock()
    monkeypatch.setattr(skill_delivery, "_svc", svc)
    app = FastAPI()
    app.include_router(skill_delivery.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = await client.post(
            "/skill-delivery/records",
            json={
                "tenant_id": TENANT,
                "collection": "other",
                "doc_id": "record",
                "data": {"approved_by": PERSON},
                "system": False,
            },
        )
    assert result.status_code == 200, result.text
    svc.document_upsert.assert_awaited_once_with(
        tenant_id=TENANT,
        collection=GRANTS,
        doc_id="record",
        data={"approved_by": PERSON},
        system=True,
        force=True,
    )


_METADATA_VECTORS = json.loads(
    (Path(__file__).parent / "fixtures" / "skill-delivery-metadata.json").read_text()
)


@pytest.mark.parametrize("vector", _METADATA_VECTORS, ids=lambda vector: vector["id"])
async def test_shared_metadata_vector_matches_approved_wire_artifact(delivery, vector):
    delivery[2].rows[0]["data"].update(vector["input"])
    response = await approve(delivery, expected_artifact_hash=vector["artifact_hash"])
    assert response.status_code == 200, response.text
    delivery[1]["auth"] = principal("owner")
    result = payload(await manifest(delivery))
    assert len(result["documents"]) == 1
    artifact = result["documents"][0]
    assert artifact["data"] == vector["canonical"]
    assert artifact["delivery"]["artifact_hash"] == vector["artifact_hash"]
    assert artifact["delivery"]["bytes"] == vector["bytes"]
    assert rendered(artifact["data"]) == vector["artifact"]


@pytest.mark.parametrize(
    "bad_id",
    ["Capitalized", "other/safe-delivery", "x" * 101, "agent/safe-delivery\n", None],
)
async def test_unsafe_identity_does_not_block_valid_approved_delivery(
    delivery, caplog, bad_id
):
    assert (await approve(delivery)).status_code == 200
    broken = doc("broken")
    broken["doc_id"] = bad_id
    delivery[2].rows.append(broken)
    delivery[1]["auth"] = principal("owner")
    result = payload(await manifest(delivery))
    assert [r["doc_id"] for r in result["documents"]] == [SLUG]
    assert not result["held"]
    assert result["prune_allowed"] is False
    assert any(
        "Skipping unsafe skill identity" in r.message and TENANT in r.message
        for r in caplog.records
    )


async def test_foreign_catalog_identity_still_fails_closed(delivery):
    row = doc("Capitalized")
    row["tenant_id"] = "foreign"
    delivery[2].query_documents.side_effect = None
    delivery[2].query_documents.return_value = [row]
    response = await manifest(delivery)
    assert response.status_code == 409, response.text
    delivery[2].read_skill_delivery_records.assert_not_awaited()


async def test_stale_current_pointers_do_not_count_against_active_delivery(delivery):
    assert (await approve(delivery)).status_code == 200
    current = next(
        row["data"]
        for row in delivery[2].rows
        if row["data"].get("record_type") == "current"
    )
    for n in range(5000):
        stale_id = f"agent/deleted-{n}"
        key = "current/" + hashlib.sha256(stale_id.encode()).hexdigest()
        delivery[2].rows.append(
            doc(key, GRANTS, **{**current, "skill_doc_id": stale_id})
        )
    delivery[1]["auth"] = principal("owner")
    result = payload(await manifest(delivery))
    assert [r["doc_id"] for r in result["documents"]] == [SLUG]
    assert result["prune_allowed"] is True
    assert all(
        c.args[0]["collection"] == "skills"
        for c in delivery[2].query_documents.await_args_list
    )
    requested = [
        key
        for c in delivery[2].read_skill_delivery_records.await_args_list
        for key in c.kwargs["doc_ids"]
    ]
    assert requested == ["current/" + hashlib.sha256(SLUG.encode()).hexdigest()]


async def test_active_approval_reads_are_exact_and_batched(delivery):
    delivery[2].rows = [doc(f"agent/live-{n}") for n in range(130)] + [
        doc("agent/inactive", status="quarantined")
    ]
    result = payload(await manifest(delivery))
    assert len(result["held"]) == 130
    calls = delivery[2].read_skill_delivery_records.await_args_list
    assert len(calls) == 3
    keys = []
    for call in calls:
        assert len(call.kwargs["doc_ids"]) <= 64
        assert call.kwargs["tenant_id"] == TENANT
        assert call.kwargs["node_id"] == NODE
        assert call.kwargs["owner_principal"] == f"agent:{OWNER}"
        keys.extend(call.kwargs["doc_ids"])
    assert len(set(keys)) == 130
    assert "current/" + hashlib.sha256(b"agent/inactive").hexdigest() not in keys


async def test_approval_batch_failure_is_not_a_partial_success(delivery):
    delivery[2].rows = [doc(f"agent/live-{n}") for n in range(65)]
    delivery[2].read_skill_delivery_records.side_effect = [
        [],
        RuntimeError("fixture failure"),
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=delivery[0], raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/skills/installable",
            json={"tenant_id": TENANT, "node_id": NODE, "delivery_version": 1},
        )
    assert response.status_code == 500, response.text
    assert "live-" not in response.text


async def test_internal_read_is_scoped_bounded_and_projects_one_recipient(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from core_api.clients.storage_client import CoreStorageClient
    from core_storage_api.routers import skill_delivery

    active = maximum = 0
    recipient = {"node_id": NODE, "owner_principal": f"agent:{OWNER}"}

    async def get(**kwargs):
        nonlocal active, maximum
        assert kwargs["tenant_id"] == TENANT and kwargs["collection"] == GRANTS
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0)
        active -= 1
        return SimpleNamespace(
            tenant_id=TENANT,
            doc_id=kwargs["doc_id"],
            data={
                "record_type": "current",
                "grant_id": "fixture-grant",
                "skill_doc_id": SLUG,
                "recipients": [
                    recipient,
                    {"node_id": PEER, "owner_principal": "agent:peer"},
                ],
            },
        )

    svc = MagicMock()
    svc.document_get_by_doc_id = AsyncMock(side_effect=get)
    monkeypatch.setattr(skill_delivery, "_svc", svc)
    app = FastAPI()
    app.include_router(skill_delivery.router)
    keys = ["current/" + hashlib.sha256(str(n).encode()).hexdigest() for n in range(64)]
    body = {
        "tenant_id": TENANT,
        "doc_ids": keys,
        "node_id": NODE,
        "owner_principal": f"agent:{OWNER}",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/skill-delivery/records/read", json=body)
        assert response.status_code == 200, response.text
        assert len(response.json()) == 64
        assert all(r["data"]["recipients"] == [recipient] for r in response.json())
        assert maximum <= 8
        assert (
            await client.post(
                "/skill-delivery/records/read",
                json={**body, "doc_ids": [*keys, keys[0]]},
            )
        ).status_code == 422
        assert (
            await client.post(
                "/skill-delivery/records/read",
                json={**body, "doc_ids": ["not-a-pointer"]},
            )
        ).status_code == 422
        assert (
            await client.post(
                "/skill-delivery/records/read", json={**body, "doc_ids": []}
            )
        ).status_code == 422
    sc = CoreStorageClient(http=MagicMock())
    sc._post = AsyncMock(return_value=[])
    await sc.read_skill_delivery_records(**body)
    sc._post.assert_awaited_once_with("/skill-delivery/records/read", body, read=False)


@pytest.mark.parametrize(
    "case",
    ["missing", "other-receiver", "not-current", "invalid-recipients", "foreign"],
)
async def test_internal_read_does_not_expose_unmatched_or_foreign_records(
    monkeypatch, case
):
    from types import SimpleNamespace

    from core_storage_api.routers import skill_delivery

    key = "current/" + hashlib.sha256(SLUG.encode()).hexdigest()
    data = {
        "record_type": "current",
        "recipients": [{"node_id": NODE, "owner_principal": f"agent:{OWNER}"}],
    }
    if case == "other-receiver":
        data["recipients"] = [{"node_id": PEER, "owner_principal": "agent:peer"}]
    elif case == "not-current":
        data["record_type"] = "history"
    elif case == "invalid-recipients":
        data["recipients"] = "malformed"
    row = (
        None
        if case == "missing"
        else SimpleNamespace(
            tenant_id="foreign" if case == "foreign" else TENANT, doc_id=key, data=data
        )
    )
    svc = MagicMock()
    svc.document_get_by_doc_id = AsyncMock(return_value=row)
    monkeypatch.setattr(skill_delivery, "_svc", svc)
    app = FastAPI()
    app.include_router(skill_delivery.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/skill-delivery/records/read",
            json={
                "tenant_id": TENANT,
                "doc_ids": [key],
                "node_id": NODE,
                "owner_principal": f"agent:{OWNER}",
            },
        )
    if case == "foreign":
        assert response.status_code == 503
    else:
        assert response.status_code == 200, response.text
        assert response.json() == []
    assert "peer" not in response.text and "recipients" not in response.text


@pytest.mark.parametrize("response", [None, {}, {"items": []}, ["not-a-record"]])
async def test_invalid_private_read_response_does_not_become_empty_approval_index(
    response,
):
    from core_api.clients.storage_client import CoreStorageClient

    sc = CoreStorageClient(http=MagicMock())
    sc._post = AsyncMock(return_value=response)
    with pytest.raises(RuntimeError, match="Invalid skill approval lookup response"):
        await sc.read_skill_delivery_records(
            tenant_id=TENANT,
            doc_ids=["current/" + "0" * 64],
            node_id=NODE,
            owner_principal=f"agent:{OWNER}",
        )


@pytest.mark.parametrize("tenant", [None, ""])
async def test_internal_read_requires_request_tenant_before_any_lookup(
    monkeypatch, tenant
):
    from core_storage_api.routers import skill_delivery

    svc = MagicMock()
    svc.document_get_by_doc_id = AsyncMock()
    monkeypatch.setattr(skill_delivery, "_svc", svc)
    app = FastAPI()
    app.include_router(skill_delivery.router)
    body = {
        "doc_ids": ["current/" + "0" * 64],
        "node_id": NODE,
        "owner_principal": f"agent:{OWNER}",
    }
    if tenant is not None:
        body["tenant_id"] = tenant
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/skill-delivery/records/read", json=body)
    assert response.status_code == 422, response.text
    svc.document_get_by_doc_id.assert_not_awaited()
