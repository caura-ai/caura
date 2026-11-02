"""A memory the non-business policy kept private stays private while it stands.

With ``governance.non_business.disposition=keep_private``, personal content is
narrowed to its writer (``scope_agent``) and marked ``nonbusiness_kept_private``.
An edit that set ``visibility`` back to ``scope_team`` or ``scope_org`` changed
no content, so nothing ran the policy again, and the row was shared after all.
While the tenant's policy still keeps such content private, widening a marked
row is refused, whoever asks: the policy is the tenant's, and an admin who
wants the row shared changes the policy. Once the policy no longer says
``keep_private``, the row widens like any other.

The fast-mode remediation narrows rows after the write; it now marks them, and
the rows derived from them, as the write-time decision does, so the edit path
can tell them apart.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from core_api.auth import AuthContext
from core_api.routes import memories as memories_routes
from core_api.schemas import MemoryUpdate
from core_api.services import agent_service, governance_remediation
from core_api.services.organization_settings import ResolvedConfig
from tests.test_memory_edit_status_gates import _row, _storage

pytestmark = pytest.mark.unit

_TENANT = "test-tenant"
_TRUST = {"alice": 1, "carol": 3}
_KEEP_PRIVATE = {"enabled": True, "disposition": "keep_private"}


def _kept_private(owner: str) -> dict:
    row = _row(owner)
    row["visibility"] = "scope_agent"
    row["metadata_"] = {
        "business_relevance": "personal",
        "nonbusiness_kept_private": True,
        "_system": {"business_relevance": "personal", "nonbusiness_kept_private": True},
    }
    return row


async def _patch(monkeypatch, caller: str | None, row: dict, fields: dict, *, nb: dict):
    """PATCH ``row`` as ``caller`` under the tenant's non-business policy ``nb``.

    Returns ``(status, stored, audit)``: the refusal's status (None when the
    edit went through), the storage write and the governance audit emitter.
    """

    async def _lookup(tenant_id, agent_id, *, read=True):
        if agent_id not in _TRUST:
            return None
        return {
            "agent_id": agent_id,
            "fleet_id": row["fleet_id"],
            "trust_level": _TRUST[agent_id],
        }

    monkeypatch.setattr(agent_service, "lookup_agent", _lookup)
    stored = _storage(monkeypatch, row)
    config = ResolvedConfig({"governance": {"non_business": nb}})

    async def _resolve_config(_tenant):
        return config

    monkeypatch.setattr(
        "core_api.services.organization_settings.resolve_config", _resolve_config
    )
    monkeypatch.setattr(memories_routes, "check_and_increment", AsyncMock())
    audit = AsyncMock()
    monkeypatch.setattr("core_api.services.memory_service.emit_governance_audit", audit)
    try:
        await memories_routes.update_memory_endpoint(
            uuid.UUID(row["id"]),
            MemoryUpdate(**fields),
            tenant_id=_TENANT,
            agent_id=None,
            metadata_mode=None,
            auth=AuthContext(tenant_id=_TENANT, agent_id=caller),
        )
    except HTTPException as refused:
        return refused.status_code, stored, audit
    return None, stored, audit


@pytest.mark.parametrize(
    ("caller", "visibility"),
    [
        ("alice", "scope_team"),
        ("carol", "scope_team"),
        ("carol", "scope_org"),
        (None, "scope_team"),
    ],
    ids=["owner-team", "trust3-team", "trust3-org", "tenant-key-team"],
)
async def test_a_kept_private_row_cannot_be_widened_while_the_policy_stands(
    monkeypatch, caller, visibility
):
    row = _kept_private(caller or "alice")

    status, stored, audit = await _patch(
        monkeypatch, caller, row, {"visibility": visibility}, nb=_KEEP_PRIVATE
    )

    assert status == 403
    stored.assert_not_awaited()
    audit.assert_awaited_once()
    assert audit.await_args.kwargs["action"] == "nonbusiness_keep_private"
    assert audit.await_args.kwargs["resource_id"] == row["id"]
    assert audit.await_args.kwargs["detail"]["refused_visibility"] == visibility


@pytest.mark.parametrize(
    "nb",
    [
        {"enabled": True, "disposition": "store"},
        {"enabled": False, "disposition": "keep_private"},
    ],
    ids=["store", "disabled"],
)
async def test_a_kept_private_row_widens_once_the_policy_no_longer_keeps_it(
    monkeypatch, nb
):
    status, stored, _ = await _patch(
        monkeypatch,
        "alice",
        _kept_private("alice"),
        {"visibility": "scope_team"},
        nb=nb,
    )

    assert status is None
    assert stored.await_args.args[2]["visibility"] == "scope_team"


@pytest.mark.parametrize(
    "fields",
    [{"title": "dentist"}, {"visibility": "scope_agent"}],
    ids=["title", "same-visibility"],
)
async def test_other_edits_of_a_kept_private_row_are_unchanged(monkeypatch, fields):
    status, stored, _ = await _patch(
        monkeypatch, "alice", _kept_private("alice"), fields, nb=_KEEP_PRIVATE
    )

    assert status is None
    stored.assert_awaited_once()


async def test_an_unmarked_private_row_widens_as_before(monkeypatch):
    row = _row("alice")
    row["visibility"] = "scope_agent"

    status, stored, _ = await _patch(
        monkeypatch, "alice", row, {"visibility": "scope_team"}, nb=_KEEP_PRIVATE
    )

    assert status is None
    assert stored.await_args.args[2]["visibility"] == "scope_team"


# --- the fast-mode remediation marks what it narrows ---------------------------


async def test_the_fast_mode_remediation_marks_the_rows_it_keeps_private(monkeypatch):
    updates: list[tuple[str, dict]] = []

    class _SC:
        async def update_memory(self, mid, tenant_id, patch):
            updates.append((mid, patch))

        async def find_children_by_parent_id(self, tenant_id, parent_id):
            return [{"id": "c1", "content": "a derived fact"}]

    monkeypatch.setattr(governance_remediation, "get_storage_client", lambda: _SC())
    monkeypatch.setattr(governance_remediation, "emit_governance_audit", AsyncMock())
    memory = {
        "id": "m1",
        "tenant_id": "t1",
        "agent_id": "a1",
        "content": "a personal errand",
        "metadata": {
            "business_relevance": "personal",
            "auto_chunked": True,
            "child_count": 1,
        },
    }

    await governance_remediation.remediate_after_enrichment(
        memory, ResolvedConfig({"governance": {"non_business": _KEEP_PRIVATE}})
    )

    assert {mid for mid, _ in updates} == {"m1", "c1"}
    for mid, patch in updates:
        assert patch["visibility"] == "scope_agent", mid
        marked = patch.get("metadata_patch") or {}
        assert marked.get("nonbusiness_kept_private") is True, (mid, patch)
        assert marked.get("_system", {}).get("nonbusiness_kept_private") is True, (
            mid,
            patch,
        )
