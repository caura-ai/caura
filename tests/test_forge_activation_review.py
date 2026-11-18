"""Forge activation requires an attributable review of the current version.

Unit contracts for staging, approval authority/provenance, write races and the
shared REST/MCP validator. Storage predicates are exercised against PostgreSQL
in test_forge_activation_storage.py rather than inferred from this fake.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from core_api import mcp_server
from core_api.app import http_exception_handler
from core_api.auth import ADMIN_KEY_CREDENTIAL, AuthContext, get_auth_context
from core_api.clients.storage_client import DocumentStatusChangedError
from core_api.routes import documents, skills_inbox
from core_api.services import organization_settings
from core_api.services.skill_lifecycle import validate_and_normalize_skill_write
from core_api.services.skill_promoter import promote_pending_candidates
from tests._mcp_test_helpers import (
    is_error_envelope,
    parse_envelope,
    stub_storage_client,
)
from tests.test_skill_lifecycle_transitions import (
    _candidate_doc,
    _doc_row,
    _fake_live_data_returns,
    _fake_poison_never,
    _patch_candidates,
)
from tests.test_skill_schema_v1 import _admin_ctx, _forge_ctx, _valid_doc
from tests.test_skills_inbox_routes import CLEAN_SCAN, FakeStorage, forge_doc

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

TENANT = "t-acme"
SLUG = "forge/summarize-oncall-handoff"
VERSION = "2026-11-17T08:00:00.123456+00:00"


def reviewed_doc(**overrides):
    doc = forge_doc(**overrides)
    doc.update(tenant_id=TENANT, collection="skills", updated_at=VERSION)
    doc["data"]["content_hash"] = (
        "sha256:" + sha256(doc["data"]["content"].encode()).hexdigest()
    )
    return doc


def principal(kind="admin"):
    base = {"tenant_id": TENANT, "org_role": "admin", "capabilities": {"read", "write"}}
    cases = {
        "admin": {"user_id": "reviewer-1", "is_person": True},
        "system": {"is_admin": True, "credential_id": ADMIN_KEY_CREDENTIAL},
        "system-unattributed": {"is_admin": True},
        "shared": {"credential_kind": "tenant_key", "credential_id": "tenant-shared"},
        "anonymous-admin": {},
        "member": {"org_role": "member", "user_id": "member-1", "is_person": True},
        "agent": {
            "agent_id": "agent-1",
            "agent_id_verified": True,
            "credential_kind": "agent",
        },
        "install": {"is_install_credential": True, "install_uuid": "host-1"},
    }
    return AuthContext(**(base | cases[kind]))


class ReviewStorage(FakeStorage):
    def __init__(self):
        super().__init__()
        self.race = None
        self.guards = []

    async def get_document(self, **kwargs):
        return deepcopy(await super().get_document(**kwargs))

    async def upsert_document(
        self,
        payload,
        *,
        expected_status=None,
        expected_content_hash=None,
        expected_updated_at=None,
    ):
        self.guards.append(
            (expected_status, expected_content_hash, expected_updated_at)
        )
        if self.race:
            race, self.race = self.race, None
            race(self.docs[payload["doc_id"]])
        stored = self.docs.get(payload["doc_id"])
        if expected_content_hash is not None and (
            not stored or stored["data"].get("content_hash") != expected_content_hash
        ):
            raise DocumentStatusChangedError("reviewed content changed")
        if expected_updated_at is not None and (
            not stored or stored.get("updated_at") != expected_updated_at
        ):
            raise DocumentStatusChangedError("reviewed row changed")
        await super().upsert_document(payload, expected_status=expected_status)


@pytest.fixture
def review(monkeypatch):
    storage = ReviewStorage()
    storage.seed(reviewed_doc())
    monkeypatch.setattr(skills_inbox, "get_storage_client", lambda: storage)
    monkeypatch.setattr(
        skills_inbox,
        "get_settings_for_display",
        AsyncMock(return_value={"skills_factory": {"enabled": True}}),
    )
    monkeypatch.setattr(
        skills_inbox, "scan_skill_doc", AsyncMock(return_value=CLEAN_SCAN)
    )
    # The approval document must remain durable even if the audit sink fails.
    audit = AsyncMock(side_effect=RuntimeError("test audit sink unavailable"))
    monkeypatch.setattr(skills_inbox, "log_action", audit)
    app = FastAPI()
    app.include_router(skills_inbox.router, prefix="/api/v1")
    app.add_exception_handler(HTTPException, http_exception_handler)
    state = {"auth": principal()}
    app.dependency_overrides[get_auth_context] = lambda: state["auth"]
    return app, state, storage


async def approve(review, body=None):
    async with AsyncClient(
        transport=ASGITransport(app=review[0]), base_url="http://test"
    ) as client:
        return await client.post(f"/api/v1/skills-inbox/{SLUG}/approve", json=body)


def expected_body(review, **extra):
    return {
        "expected_content_hash": review[2].docs[SLUG]["data"]["content_hash"],
        **extra,
    }


@pytest.mark.parametrize("legacy_flag", [False, True])
async def test_passing_candidates_always_wait_for_review(legacy_flag):
    updates = AsyncMock()
    with _patch_candidates([_doc_row("forge/clean", _candidate_doc())]):
        result = await promote_pending_candidates(
            tenant_id="t1",
            fleet_id=None,
            poison_checker=_fake_poison_never,
            live_data_fetcher=await _fake_live_data_returns(None),
            status_updater=updates,
            min_cluster_size=3,
            min_distinct_agents=3,
            freshness_window_days=14,
            auto_promote_clean=legacy_flag,
        )
    updates.assert_awaited_once_with("t1", "skills", "forge/clean", "staged")
    assert result.promoted == 1
    assert result.auto_approved == 0
    assert result.attempts[0].target_status == "staged"


async def test_the_retired_opt_in_cannot_be_set_or_advertised():
    payload = {"skills_factory": {"sentinel": {"auto_promote_clean": True}}}
    with pytest.raises(ValueError, match="auto_promote_clean"):
        organization_settings._check_keys(
            payload, organization_settings.DEFAULT_SETTINGS
        )
    displayed = organization_settings._settings_display_with_status(payload)
    assert "auto_promote_clean" not in displayed["skills_factory"]["sentinel"]


async def test_forge_approval_requires_the_hash_the_reviewer_saw(review):
    response = await approve(review)
    assert response.status_code == 422, response.text
    assert "expected_content_hash" in response.text
    assert not review[2].upserts


async def test_a_stale_review_is_refused_before_activation(review):
    response = await approve(review, {"expected_content_hash": "sha256:" + "0" * 64})
    assert response.status_code == 409, response.text
    assert not review[2].upserts


@pytest.mark.parametrize(
    "who",
    ["shared", "anonymous-admin", "system-unattributed", "member", "agent", "install"],
)
async def test_role_labels_do_not_replace_an_attributable_approver(review, who):
    review[1]["auth"] = principal(who)
    response = await approve(review)
    assert response.status_code == 403, response.text
    assert not review[2].upserts


@pytest.mark.parametrize(
    "who,actor", [("admin", "reviewer-1"), ("system", ADMIN_KEY_CREDENTIAL)]
)
@pytest.mark.parametrize("override", [False, True])
async def test_activation_and_review_provenance_are_one_guarded_write(
    review, who, actor, override
):
    review[1]["auth"] = principal(who)
    if override:
        review[2].seed(reviewed_doc(status="quarantined"))
    before = datetime.now(UTC)
    body = expected_body(review)
    if override:
        body.update(
            override_quarantine=True, reason="Reviewed this candidate's findings"
        )
    response = await approve(review, body)
    after = datetime.now(UTC)
    assert response.status_code == 200, response.text
    assert len(review[2].upserts) == 1
    data = review[2].upserts[0]["data"]
    assert data["status"] == "active"
    stamp = data["activation_approval"]
    assert stamp["approved_by"] == actor
    assert stamp["content_hash"] == body["expected_content_hash"]
    approved_at = datetime.fromisoformat(stamp["approved_at"])
    assert approved_at.utcoffset().total_seconds() == 0
    assert before.replace(microsecond=0) <= approved_at <= after
    statuses, content_hash, version = review[2].guards[0]
    assert statuses == ["quarantined" if override else "staged"]
    assert content_hash == body["expected_content_hash"]
    assert version == VERSION


@pytest.mark.parametrize("change", ["content", "metadata", "status"])
async def test_a_change_after_the_final_reload_cannot_receive_the_review(
    review, change
):
    def race(doc):
        doc["updated_at"] = "2026-11-17T08:00:01.123456+00:00"
        if change == "content":
            doc["data"].update(
                content="Different version", content_hash="sha256:" + "f" * 64
            )
        elif change == "metadata":
            doc["data"]["description"] = "A concurrent editor's description"
        else:
            doc["data"]["status"] = "rejected"

    review[2].race = race
    response = await approve(review, expected_body(review))
    assert response.status_code == 409, response.text
    assert not review[2].upserts
    assert "activation_approval" not in review[2].docs[SLUG]["data"]
    if change == "metadata":
        assert (
            review[2].docs[SLUG]["data"]["description"]
            == "A concurrent editor's description"
        )


async def test_a_stored_hash_that_does_not_match_the_bytes_is_not_reviewed(review):
    review[2].docs[SLUG]["data"]["content"] += (
        "\nChanged without updating the stored hash."
    )
    response = await approve(review, expected_body(review))
    assert response.status_code == 409, response.text
    assert not review[2].upserts


async def test_two_approvals_do_not_replace_the_first_review(review):
    body = expected_body(review)
    first = await approve(review, body)
    assert first.status_code == 200, first.text
    saved = deepcopy(review[2].docs[SLUG])
    review[1]["auth"] = principal("system")
    second = await approve(review, body)
    assert second.status_code == 409, second.text
    assert review[2].docs[SLUG] == saved
    assert len(review[2].upserts) == 1


async def test_legacy_active_skills_are_not_backfilled_or_deactivated(review):
    review[2].seed(reviewed_doc(status="active"))
    saved = deepcopy(review[2].docs[SLUG])
    response = await approve(review)
    assert response.status_code == 409, response.text
    assert review[2].docs[SLUG] == saved
    assert not review[2].upserts


async def test_generic_admin_writes_cannot_relabel_and_activate_stored_forge_content():
    live = reviewed_doc()
    with pytest.raises(HTTPException) as exc:
        await validate_and_normalize_skill_write(
            _valid_doc(source="agent", status="active"),
            ctx=_admin_ctx(),
            live_skill_doc=live,
        )
    assert exc.value.status_code == 403


async def test_internal_forge_writes_cannot_skip_the_review_either():
    with pytest.raises(HTTPException) as exc:
        await validate_and_normalize_skill_write(
            _valid_doc(source="forge", status="active"),
            ctx=_forge_ctx(is_admin=True),
            live_skill_doc=None,
        )
    assert exc.value.status_code == 403


async def test_review_fields_are_not_caller_owned_even_for_a_staged_write():
    forged = {
        "approved_by": "someone-else",
        "approved_at": VERSION,
        "content_hash": "invented",
    }
    with pytest.raises(HTTPException) as exc:
        await validate_and_normalize_skill_write(
            _valid_doc(status="staged", activation_approval=forged),
            ctx=_admin_ctx(),
        )
    assert exc.value.status_code in (403, 422)


@pytest.mark.parametrize("key", ["activation_approval", "quarantine_override"])
async def test_admin_can_echo_existing_review_provenance_on_a_non_forge_skill(key):
    stamp = {"approved_by": "original-reviewer", "approved_at": VERSION}
    data = _valid_doc(source="manual", status="active", **{key: stamp})
    live = {"doc_id": "test-skill", "data": deepcopy(data)}
    out, _ = await validate_and_normalize_skill_write(
        deepcopy(data),
        ctx=_admin_ctx(),
        live_skill_doc=live,
    )
    assert out[key] == stamp
    assert live["data"][key] == stamp
    assert out["status"] == "active"


@pytest.mark.parametrize("key", ["activation_approval", "quarantine_override"])
@pytest.mark.parametrize("case", ["changed", "no-document", "no-key", "new-null"])
async def test_generic_writes_cannot_create_or_change_review_provenance(key, case):
    stamp = {"approved_by": "original-reviewer", "approved_at": VERSION}
    data = _valid_doc(source="manual", status="active")
    live = {"doc_id": "test-skill", "data": {**data, key: deepcopy(stamp)}}
    data[key] = {**stamp, "approved_by": "invented-reviewer"}
    if case == "no-document":
        live = None
    elif case in ("no-key", "new-null"):
        live["data"].pop(key)
        data[key] = None if case == "new-null" else deepcopy(stamp)
    with pytest.raises(HTTPException) as exc:
        await validate_and_normalize_skill_write(
            data, ctx=_admin_ctx(), live_skill_doc=live
        )
    assert exc.value.status_code == 403


@pytest.mark.parametrize("key", ["activation_approval", "quarantine_override"])
async def test_unchanged_provenance_does_not_enable_generic_forge_activation(key):
    stamp = {"approved_by": "original-reviewer", "approved_at": VERSION}
    live = reviewed_doc(**{key: stamp})
    with pytest.raises(HTTPException) as exc:
        await validate_and_normalize_skill_write(
            _valid_doc(status="active", **{key: deepcopy(stamp)}),
            ctx=_admin_ctx(),
            live_skill_doc=live,
        )
    assert exc.value.status_code == 403


@pytest.mark.parametrize("key", ["activation_approval", "quarantine_override"])
async def test_mcp_can_echo_existing_provenance_on_an_editable_staged_skill(
    mcp_env, monkeypatch, key
):
    stamp = {"approved_by": "original-reviewer", "approved_at": VERSION}
    data = _valid_doc(status="staged", **{key: stamp})
    monkeypatch.setattr(
        mcp_server, "get_settings_for_display", AsyncMock(return_value={})
    )
    sc = stub_storage_client(
        monkeypatch,
        get_document={
            "doc_id": "agent/test-skill",
            "tenant_id": "test-tenant",
            "data": deepcopy(data),
        },
        upsert_document_xmax={"xmax": 1},
    )
    out = await mcp_server.caura_doc(
        op="write",
        collection="skills",
        doc_id="agent/test-skill",
        data=deepcopy(data),
    )
    assert not is_error_envelope(out), parse_envelope(out)
    assert sc.upsert_document_xmax.await_args.args[0]["data"][key] == stamp


@pytest.mark.parametrize("key", ["activation_approval", "quarantine_override"])
async def test_rest_admin_can_round_trip_a_non_forge_active_skill(monkeypatch, key):
    stamp = {"approved_by": "original-reviewer", "approved_at": VERSION}
    data = _valid_doc(source="manual", status="active", **{key: stamp})
    row = {
        "id": "00000000-0000-0000-0000-000000000001",
        "tenant_id": TENANT,
        "collection": "skills",
        "doc_id": "test-skill",
        "data": deepcopy(data),
        "created_at": VERSION,
        "updated_at": VERSION,
    }
    monkeypatch.setattr(
        documents, "get_settings_for_display", AsyncMock(return_value={})
    )

    async def pii(**kwargs):
        return kwargs["data"]

    monkeypatch.setattr(documents, "apply_pii_policy_to_fields", pii)
    monkeypatch.setattr(documents, "log_action", AsyncMock())
    monkeypatch.setattr(
        "core_api.services.doc_memory.safe_sync_doc_memory", AsyncMock()
    )
    sc = stub_storage_client(
        monkeypatch, get_document=row, upsert_document_xmax={"xmax": 1}
    )
    monkeypatch.setattr(documents, "get_storage_client", lambda: sc)
    body = documents.DocWriteRequest(
        tenant_id=TENANT,
        collection="skills",
        doc_id=row["doc_id"],
        data=deepcopy(data),
    )
    out = await documents._upsert_document_claimed(body, principal(), None, None)
    assert out.data[key] == stamp
    assert sc.upsert_document_xmax.await_args.args[0]["data"][key] == stamp


@pytest.mark.parametrize("existing", [False, True])
async def test_mcp_cannot_activate_a_forge_namespace_by_changing_source(
    mcp_env, monkeypatch, existing
):
    monkeypatch.setattr(
        mcp_server, "get_settings_for_display", AsyncMock(return_value={})
    )
    live = reviewed_doc() if existing else None
    if live:
        live["tenant_id"] = "test-tenant"
    sc = stub_storage_client(monkeypatch, get_document=live)
    sc.upsert_document_xmax = AsyncMock(return_value={"xmax": 0})
    out = await mcp_server.caura_doc(
        op="write",
        collection="skills",
        doc_id=SLUG,
        data=_valid_doc(source="agent", status="active"),
    )
    assert is_error_envelope(out), parse_envelope(out)
    assert parse_envelope(out)["error"]["code"] == "FORBIDDEN"
    sc.upsert_document_xmax.assert_not_awaited()


async def test_mcp_cannot_forge_review_provenance_on_a_staged_skill(
    mcp_env, monkeypatch
):
    monkeypatch.setattr(
        mcp_server, "get_settings_for_display", AsyncMock(return_value={})
    )
    sc = stub_storage_client(
        monkeypatch, get_document=None, upsert_document_xmax={"xmax": 0}
    )
    out = await mcp_server.caura_doc(
        op="write",
        collection="skills",
        doc_id="agent/test-skill",
        data=_valid_doc(
            status="staged", activation_approval={"approved_by": "invented"}
        ),
    )
    assert is_error_envelope(out), parse_envelope(out)
    assert parse_envelope(out)["error"]["code"] in ("FORBIDDEN", "INVALID_ARGUMENTS")
    sc.upsert_document_xmax.assert_not_awaited()


@pytest.mark.parametrize("existing", [False, True])
async def test_rest_admin_cannot_activate_a_forge_namespace_by_changing_source(
    monkeypatch, existing
):
    monkeypatch.setattr(
        documents, "get_settings_for_display", AsyncMock(return_value={})
    )

    async def pii(**kwargs):
        return kwargs["data"]

    monkeypatch.setattr(documents, "apply_pii_policy_to_fields", pii)
    sc = stub_storage_client(
        monkeypatch, get_document=reviewed_doc() if existing else None
    )
    row = {
        **reviewed_doc(),
        "id": "00000000-0000-0000-0000-000000000001",
        "created_at": VERSION,
    }
    sc.get_document.side_effect = [reviewed_doc() if existing else None, row]
    sc.upsert_document_xmax = AsyncMock(return_value={"xmax": 0})
    monkeypatch.setattr(
        "core_api.services.doc_memory.safe_sync_doc_memory", AsyncMock()
    )
    monkeypatch.setattr(documents, "log_action", AsyncMock())
    monkeypatch.setattr(documents, "get_storage_client", lambda: sc)
    body = documents.DocWriteRequest(
        tenant_id=TENANT,
        collection="skills",
        doc_id=SLUG,
        data=_valid_doc(source="agent", status="active"),
    )
    with pytest.raises(HTTPException) as exc:
        await documents._upsert_document_claimed(body, principal(), None, None)
    assert exc.value.status_code == 403
    sc.upsert_document_xmax.assert_not_awaited()
