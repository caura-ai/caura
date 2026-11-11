"""Node-reported installation results require bound approvals and durable acceptance."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from tests.test_skill_delivery import (
    GRANTS,
    NODE,
    OWNER,
    PEER,
    SLUG,
    TENANT,
    approve,
    call,
    manifest,
    principal,
)
from tests.test_skill_delivery import (
    delivery as delivery,
)

pytestmark = pytest.mark.unit


@pytest.fixture
async def installation(delivery):
    grant = await approve(delivery)
    assert grant.status_code == 200, grant.text
    delivery[1]["auth"] = principal("owner")
    delivery[2].create_audit_logs_bulk = AsyncMock(
        side_effect=lambda events: {"ok": True, "count": len(events)}
    )
    return delivery, grant.json()


def event(grant, **changes):
    return {
        "event_id": str(uuid4()),
        "skill_doc_id": SLUG,
        "grant_id": grant["grant_id"],
        "artifact_hash": grant["artifact_hash"],
        "outcome": "installed",
        "target_id": str(uuid4()),
        "occurred_at": "2026-11-11T10:00:00Z",
        **changes,
    }


async def submit(delivery, events, **changes):
    return await call(
        delivery,
        "POST",
        "/skills/installations",
        json={
            "tenant_id": TENANT,
            "reporting_version": 1,
            "node_id": NODE,
            "events": events,
            **changes,
        },
    )


@pytest.mark.parametrize("outcome", ["installed", "updated", "removed"])
async def test_bound_result_is_persisted_with_server_attribution(installation, outcome):
    delivery, grant = installation
    ev = event(grant, outcome=outcome)
    before = datetime.now(UTC)
    response = await submit(delivery, [ev])
    after = datetime.now(UTC)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "reporting_version": 1,
        "node_id": NODE,
        "accepted_event_ids": [ev["event_id"]],
    }
    delivery[2].create_audit_logs_bulk.assert_awaited_once()
    stored = delivery[2].create_audit_logs_bulk.await_args.args[0]
    assert len(stored) == 1
    row = stored[0]
    assert row["tenant_id"] == TENANT
    assert row["agent_id"] == OWNER
    assert row["action"] == "skill.installation"
    assert row["resource_type"] == "skill"
    assert row["client_event_id"] != ev["event_id"], "namespace IDs by bound node"
    detail = row["detail"]
    assert detail["node_id"] == NODE
    assert detail["owner_principal"] == f"agent:{OWNER}"
    assert detail["reported_by"] == f"agent:{OWNER}"
    assert detail["source"] == "node_reported"
    for key, value in ev.items():
        if key == "occurred_at":
            assert datetime.fromisoformat(detail[key]) == datetime.fromisoformat(value)
        else:
            assert detail[key] == value
    received = datetime.fromisoformat(detail["received_at"])
    assert before <= received <= after
    assert not {"content", "path", "directory", "recipients"} & detail.keys()
    assert "content" not in json.dumps(response.json())


async def test_valid_result_waits_for_storage_before_acknowledging(installation):
    delivery, grant = installation
    entered, release = asyncio.Event(), asyncio.Event()

    async def persist(events):
        entered.set()
        await release.wait()
        return {"ok": True, "count": len(events)}

    delivery[2].create_audit_logs_bulk.side_effect = persist
    pending = asyncio.create_task(submit(delivery, [event(grant)]))
    witness = asyncio.create_task(entered.wait())
    try:
        done, _ = await asyncio.wait(
            {pending, witness}, timeout=5, return_when=asyncio.FIRST_COMPLETED
        )
        if pending in done:
            response = await pending
            pytest.fail(
                f"Intake returned before durable storage: {response.status_code} {response.text}"
            )
        assert witness in done, "intake did not reach storage"
        assert not pending.done(), "queue acceptance is not durable acknowledgment"
    finally:
        witness.cancel()
        release.set()
        response = await pending
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("failure", ["exception", "refused", "missing", "partial"])
async def test_storage_failure_never_acknowledges_results(installation, failure):
    delivery, grant = installation
    if failure == "exception":
        delivery[2].create_audit_logs_bulk.side_effect = RuntimeError(
            "fixture unavailable"
        )
    else:
        delivery[2].create_audit_logs_bulk.side_effect = None
        delivery[2].create_audit_logs_bulk.return_value = {
            "refused": {"ok": False, "count": 1},
            "missing": {},
            "partial": {"ok": True, "count": 0},
        }[failure]
    response = await submit(delivery, [event(grant)])
    assert response.status_code == 503, response.text
    assert "accepted_event_ids" not in response.json()


async def test_replay_keeps_storage_id_stable(installation):
    delivery, grant = installation
    ev = event(grant)
    for _ in range(2):
        response = await submit(delivery, [ev])
        assert response.status_code == 200, response.text
        assert response.json()["accepted_event_ids"] == [ev["event_id"]]
    calls = delivery[2].create_audit_logs_bulk.await_args_list
    assert (
        calls[0].args[0][0]["client_event_id"] == calls[1].args[0][0]["client_event_id"]
    )


async def test_same_client_id_on_another_bound_node_does_not_dedupe(installation):
    delivery, _ = installation
    delivery[2].nodes[PEER]["owner_principal"] = f"agent:{OWNER}"
    grant = await approve(delivery, [NODE, PEER])
    assert grant.status_code == 200
    delivery[1]["auth"] = principal("owner")
    ev = event(grant.json())
    first = await submit(delivery, [ev])
    second = await submit(delivery, [ev], node_id=PEER)
    assert first.status_code == second.status_code == 200
    calls = delivery[2].create_audit_logs_bulk.await_args_list
    assert (
        calls[0].args[0][0]["client_event_id"] != calls[1].args[0][0]["client_event_id"]
    )


async def test_superseded_approval_still_validates_offline_results(installation):
    delivery, historical = installation
    delivery[2].rows[0]["data"]["content"] = "# A later artifact\n"
    current = await approve(delivery)
    assert current.status_code == 200
    assert current.json()["grant_id"] != historical["grant_id"]
    delivery[1]["auth"] = principal("owner")
    response = await submit(delivery, [event(historical)])
    assert response.status_code == 200, response.text
    lookups = delivery[2].get_document.call_args_list
    assert any(
        call.args[1:] == (GRANTS, historical["grant_id"])
        and call.kwargs.get("read") is False
        for call in lookups
    ), "resolve the exact immutable version from the primary"


@pytest.mark.parametrize(
    "kind", ["peer", "unverified", "member", "read-only", "foreign-tenant", "rebound"]
)
async def test_unbound_credentials_cannot_report_for_node(installation, kind):
    delivery, grant = installation
    changes = {}
    if kind in {"peer", "unverified", "member"}:
        delivery[1]["auth"] = principal(kind)
    elif kind == "read-only":
        delivery[1]["auth"].is_read_only = True
    elif kind == "foreign-tenant":
        changes["tenant_id"] = "another-tenant"
    else:
        delivery[2].nodes[NODE]["owner_principal"] = "agent:new-owner"
    response = await submit(delivery, [event(grant)], **changes)
    assert response.status_code == 403, response.text
    delivery[2].create_audit_logs_bulk.assert_not_awaited()


@pytest.mark.parametrize("binding", ["install", "tenant"])
async def test_other_explicit_node_credential_paths_are_supported(delivery, binding):
    from common.constants import NODE_PRINCIPAL_TENANT
    from core_api.auth import AuthContext

    owner = "install:install-a" if binding == "install" else NODE_PRINCIPAL_TENANT
    delivery[2].nodes[NODE]["owner_principal"] = owner
    grant = await approve(delivery)
    assert grant.status_code == 200
    delivery[1]["auth"] = (
        principal("install")
        if binding == "install"
        else AuthContext(TENANT, credential_kind="tenant")
    )
    delivery[2].create_audit_logs_bulk = AsyncMock(
        return_value={"ok": True, "count": 1}
    )
    response = await submit(delivery, [event(grant.json())])
    assert response.status_code == 200, response.text
    detail = delivery[2].create_audit_logs_bulk.await_args.args[0][0]["detail"]
    assert detail["owner_principal"] == owner


@pytest.mark.parametrize(
    "damage",
    [
        "absent",
        "foreign",
        "recipient",
        "malformed-recipients",
        "hash",
        "skill",
        "version",
    ],
)
async def test_result_must_match_immutable_approval(installation, damage):
    delivery, grant = installation
    ev = event(grant)
    row = next(row for row in delivery[2].rows if row["doc_id"] == grant["grant_id"])
    if damage == "absent":
        delivery[2].rows.remove(row)
    elif damage == "foreign":
        row["tenant_id"] = "another-tenant"
    elif damage == "recipient":
        row["data"]["recipients"] = [
            {"node_id": PEER, "owner_principal": f"agent:{OWNER}"}
        ]
    elif damage == "malformed-recipients":
        row["data"]["recipients"] = None
    elif damage == "hash":
        ev["artifact_hash"] = "sha256:" + "0" * 64
    elif damage == "skill":
        ev["skill_doc_id"] = "agent/different-skill"
    else:
        row["data"]["delivery_version"] = 99
    response = await submit(delivery, [ev])
    assert response.status_code == 409, response.text
    delivery[2].create_audit_logs_bulk.assert_not_awaited()


async def test_whole_batch_is_validated_before_persistence(installation):
    delivery, grant = installation
    response = await submit(
        delivery, [event(grant), event(grant, artifact_hash="sha256:" + "0" * 64)]
    )
    assert response.status_code == 409, response.text
    delivery[2].create_audit_logs_bulk.assert_not_awaited()


@pytest.mark.parametrize("size,expected", [(0, 422), (64, 200), (65, 422)])
async def test_event_batch_has_a_hard_bound(installation, size, expected):
    delivery, grant = installation
    response = await submit(delivery, [event(grant) for _ in range(size)])
    assert response.status_code == expected, response.text
    assert delivery[2].create_audit_logs_bulk.await_count == (expected == 200)


@pytest.mark.parametrize(
    "extra",
    [
        {"agent_id": "asserted-owner"},
        {"owner_principal": f"agent:{OWNER}"},
        {"received_at": "2020-01-01T00:00:00Z"},
        {"path": "/private/fixture/SKILL.md"},
        {"content": "# Do not transmit raw content"},
        {"fleet_id": "asserted-fleet"},
    ],
)
async def test_reported_event_cannot_supply_server_identity_or_private_data(
    installation, extra
):
    delivery, grant = installation
    response = await submit(delivery, [event(grant, **extra)])
    assert response.status_code == 422, response.text
    delivery[2].create_audit_logs_bulk.assert_not_awaited()


@pytest.mark.parametrize(
    "extra",
    [
        {"event_id": "not-a-uuid"},
        {"target_id": "/private/path"},
        {"grant_id": "current/" + "0" * 64},
        {"skill_doc_id": "agent/../unsafe"},
        {"outcome": "failed"},
        {"occurred_at": "not-a-time"},
    ],
)
async def test_malformed_result_never_reaches_storage(installation, extra):
    delivery, grant = installation
    response = await submit(delivery, [event(grant, **extra)])
    assert response.status_code == 422, response.text
    delivery[2].create_audit_logs_bulk.assert_not_awaited()


async def test_conflicting_ids_in_one_batch_are_refused(installation):
    delivery, grant = installation
    ev = event(grant)
    changed = {**deepcopy(ev), "outcome": "removed"}
    response = await submit(delivery, [ev, changed])
    assert response.status_code == 422, response.text
    delivery[2].create_audit_logs_bulk.assert_not_awaited()


async def test_invalid_reporting_version_does_not_acknowledge(installation):
    delivery, grant = installation
    response = await submit(delivery, [event(grant)], reporting_version=99)
    assert response.status_code == 422, response.text
    delivery[2].create_audit_logs_bulk.assert_not_awaited()


@pytest.mark.parametrize("extra", [{"agent_id": OWNER}, {"fleet_id": "asserted-fleet"}])
async def test_request_envelope_cannot_assert_receiver_authority(installation, extra):
    delivery, grant = installation
    response = await submit(delivery, [event(grant)], **extra)
    assert response.status_code == 422, response.text
    delivery[2].create_audit_logs_bulk.assert_not_awaited()


async def test_existing_approved_delivery_still_works(installation):
    delivery, _ = installation
    response = await manifest(delivery)
    assert response.status_code == 200, response.text
    assert response.json()["documents"][0]["doc_id"] == SLUG
    delivery[2].create_audit_logs_bulk.assert_not_awaited()


@pytest.mark.integration
@pytest.mark.parametrize("lost_ack", [False, True])
async def test_results_persist_and_replay_through_real_storage(
    client, monkeypatch, lost_ack
):
    """Real route -> HTTP storage bridge -> PostgreSQL, including lost ACK."""
    from sqlalchemy import select

    from common.models.audit import AuditLog
    from core_api.clients.storage_client import get_storage_client
    from core_storage_api.services.postgres_service import PostgresService, get_session
    from tests.conftest import get_test_auth, new_tenant_id
    from tests.test_skill_delivery import digest, rendered

    tenant, headers = get_test_auth(new_tenant_id())
    sc = get_storage_client()
    heartbeat = await client.post(
        "/api/v1/fleet/heartbeat",
        json={
            "tenant_id": tenant,
            "node_name": "installation-integration",
            "agents": [],
        },
        headers=headers,
    )
    assert heartbeat.status_code == 200, heartbeat.text
    node = heartbeat.json()["node_id"]
    data = {
        "name": "integration",
        "description": "Reviewed fixture",
        "content": "# Version one\n",
        "status": "active",
    }
    await sc.upsert_document(
        {"tenant_id": tenant, "collection": "skills", "doc_id": SLUG, "data": data}
    )
    grant = await client.post(
        f"/api/v1/skills/{SLUG}/delivery",
        json={
            "tenant_id": tenant,
            "node_ids": [node],
            "expected_artifact_hash": digest(rendered(data)),
        },
        headers=headers,
    )
    assert grant.status_code == 200, grant.text
    # Publish a later version before the delayed report arrives. The old
    # immutable approval remains authoritative for its original materialization.
    data["content"] = "# Version two\n"
    await sc.upsert_document(
        {"tenant_id": tenant, "collection": "skills", "doc_id": SLUG, "data": data}
    )
    newer = await client.post(
        f"/api/v1/skills/{SLUG}/delivery",
        json={
            "tenant_id": tenant,
            "node_ids": [node],
            "expected_artifact_hash": digest(rendered(data)),
        },
        headers=headers,
    )
    assert newer.status_code == 200, newer.text
    ev = event(grant.json())
    payload = {
        "tenant_id": tenant,
        "node_id": node,
        "reporting_version": 1,
        "events": [ev],
    }
    original = sc.create_audit_logs_bulk

    if lost_ack:

        async def commit_then_lose_ack(events):
            await original(events)
            raise TimeoutError("fixture acknowledgment lost after commit")

        with monkeypatch.context() as patch:
            patch.setattr(sc, "create_audit_logs_bulk", commit_then_lose_ack)
            refused = await client.post(
                "/api/v1/skills/installations", json=payload, headers=headers
            )
        assert refused.status_code == 503, refused.text
        assert "accepted_event_ids" not in refused.json()

    for _ in range(2):
        accepted = await client.post(
            "/api/v1/skills/installations", json=payload, headers=headers
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["accepted_event_ids"] == [ev["event_id"]]
    async with get_session() as session:
        rows = (
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.tenant_id == tenant,
                        AuditLog.action == "skill.installation",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1, (
        "a lost acknowledgment/replay must not append duplicate results"
    )
    assert rows[0].detail["grant_id"] == grant.json()["grant_id"]
    assert rows[0].detail["node_id"] == node
    assert rows[0].detail["source"] == "node_reported"
    verification = await PostgresService().audit_verify_chain(tenant)
    assert verification["valid"] is True, verification
