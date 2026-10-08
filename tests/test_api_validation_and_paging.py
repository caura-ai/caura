"""REST answers a bad request with a 4xx, and reaches what it lets a caller write.

The core-api half of the 2026-10-01 audit's API validation and pagination batch.
Every case named here failed on main:

- M-13: an agent id with a ``/`` never resolved, so every write re-created the
  agent, reset its trust and audited a fresh registration; ``?`` and ``#`` cut
  the storage path short and looked up a different agent.
- M-28: a person reviewing a conflict was refused unless an ``mcp-agent`` row
  existed at trust 2, and once one did, every reviewer was filed as it.
- M-30: a document whose id has a ``/`` could be written but not read or
  deleted by id.
- L-20 to L-23: the agent fleet PATCH took any JSON type, an agent delete was
  audited before it ran, the entity list could not page, and an admin list or
  stats call naming no tenant was a 500.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from core_api.auth import AuthContext
from core_api.clients.storage_client import CoreStorageClient
from core_api.config import settings
from core_api.routes import agents, conflicts, entities
from core_api.schemas import ConflictResolveRequest
from core_api.services import agent_service
from tests.conftest import get_test_auth, uid


@pytest.fixture
def as_auth():
    """Serve the next requests as the credential built from these kwargs."""
    from core_api.app import app
    from core_api.auth import get_auth_context
    from core_api.tenant_context import set_current_tenant

    def _install(**kwargs):
        async def _dep():
            if kwargs.get("tenant_id"):
                set_current_tenant(kwargs["tenant_id"])
            return AuthContext(**kwargs)

        app.dependency_overrides[get_auth_context] = _dep

    yield _install
    app.dependency_overrides.pop(get_auth_context, None)


# ── M-13: agent ids in storage paths ─────────────────────────────────────

_ODD = "bot?v2#x"
_ESCAPED = "bot%3Fv2%23x"


@pytest.mark.parametrize(
    ("call", "transport", "path"),
    [
        (lambda sc: sc.get_agent(_ODD, "t"), "_get", ""),
        (lambda sc: sc.get_search_profile(_ODD, "t"), "_get", "/search-profile"),
        (lambda sc: sc.update_trust_level(_ODD, {}), "_patch", "/trust-level"),
        (lambda sc: sc.update_agent_fleet(_ODD, {}), "_patch", "/fleet"),
        (
            lambda sc: sc.update_search_profile(_ODD, "t", {}),
            "_patch",
            "/search-profile",
        ),
        (
            lambda sc: sc.reset_search_profile(_ODD, "t"),
            "_post_optional",
            "/search-profile/reset",
        ),
        (lambda sc: sc.delete_agent(_ODD, "t"), "_delete", ""),
    ],
    ids=["get", "get-profile", "trust", "fleet", "tune", "reset", "delete"],
)
async def test_m13_an_agent_id_is_escaped_in_its_storage_path(call, transport, path):
    """Unescaped, httpx reads ``?`` as the start of the query and ``#`` as a
    fragment, so ``bot?v2`` resolved to agent ``bot``: another agent's row,
    trust and fleet."""
    sc = CoreStorageClient()
    seen: list[str] = []

    async def _record(p, *args, **kwargs):
        seen.append(p)

    setattr(sc, transport, _record)
    await call(sc)

    assert seen == [f"/agents/{_ESCAPED}{path}"]


async def test_m13_an_agent_id_with_a_slash_is_refused_not_registered(monkeypatch):
    """Storage addresses an agent by one path segment, and the server decodes
    an escaped ``/`` before routing, so such an id can never be found. Every
    write took the create path: trust reset, an ``agent_registered`` audit row,
    and an agent no admin could approve or manage."""
    sc = MagicMock()
    sc.get_agent = AsyncMock(return_value=None)
    sc.create_or_update_agent = AsyncMock(return_value={})
    monkeypatch.setattr(agent_service, "get_storage_client", lambda: sc)
    audit = AsyncMock()
    monkeypatch.setattr(agent_service, "log_action", audit)

    with pytest.raises(HTTPException) as refused:
        await agent_service.get_or_create_agent(
            tenant_id="t", agent_id="sales/eu-bot", require_approval=False
        )

    assert refused.value.status_code == 422
    assert "/" in str(refused.value.detail)
    sc.get_agent.assert_not_awaited()
    sc.create_or_update_agent.assert_not_awaited()
    audit.assert_not_awaited()


# ── M-28: who may review a conflict ──────────────────────────────────────

_NOT_REGISTERED = (None, True, None)
_TRUST_2 = (2, False, None)


def _reviewing(monkeypatch, trust) -> AsyncMock:
    sc = AsyncMock()
    sc.resolve_memory_conflict = AsyncMock(
        return_value={
            "id": "00000000-0000-0000-0000-000000000003",
            "tenant_id": "t1",
            "new_memory_id": "00000000-0000-0000-0000-000000000001",
            "old_memory_id": "00000000-0000-0000-0000-000000000002",
            "relationship": "exact_value",
            "review_status": "resolved",
        }
    )
    monkeypatch.setattr(conflicts, "get_storage_client", lambda: sc)
    monkeypatch.setattr(conflicts, "_require_trust", AsyncMock(return_value=trust))
    monkeypatch.setattr(conflicts, "log_action", AsyncMock())
    return sc


async def _resolve(auth: AuthContext) -> None:
    body = ConflictResolveRequest(tenant_id="t1", review_status="resolved")
    await conflicts.resolve_conflict("c1", body, auth=auth)


def _filed_as(sc: AsyncMock) -> str | None:
    return sc.resolve_memory_conflict.await_args.args[1]["resolved_by"]


@pytest.mark.parametrize(
    ("kwargs", "reviewer"),
    [
        (
            {"tenant_id": "t1", "is_person": True, "user_id": "u-7"},
            "u-7",
        ),
        ({"tenant_id": None, "is_admin": True}, "admin"),
    ],
    ids=["person", "admin key"],
)
async def test_m28_a_person_or_the_admin_reviews_without_an_agent_row(
    monkeypatch, kwargs, reviewer
):
    monkeypatch.setattr(settings, "is_standalone", False)
    sc = _reviewing(monkeypatch, _NOT_REGISTERED)

    await _resolve(AuthContext(**kwargs))

    assert _filed_as(sc) == reviewer


async def test_m28_the_standalone_operator_reviews_without_an_agent_row(monkeypatch):
    monkeypatch.setattr(settings, "is_standalone", True)
    sc = _reviewing(monkeypatch, _NOT_REGISTERED)

    await _resolve(AuthContext(tenant_id="t1", org_role="admin"))

    assert _filed_as(sc) is not None


async def test_m28_a_tenant_key_naming_no_agent_is_not_filed_as_mcp_agent(
    monkeypatch,
):
    """Even with an ``mcp-agent`` row at trust 2: a machine key that names no
    agent must not file a decision under a name every such key shares."""
    monkeypatch.setattr(settings, "is_standalone", False)
    sc = _reviewing(monkeypatch, _TRUST_2)

    with pytest.raises(HTTPException) as refused:
        await _resolve(AuthContext(tenant_id="t1"))

    assert refused.value.status_code == 403
    sc.resolve_memory_conflict.assert_not_awaited()


async def test_m28_an_agent_is_still_held_to_trust_2_and_filed_as_itself(
    monkeypatch,
):
    monkeypatch.setattr(settings, "is_standalone", False)
    sc = _reviewing(monkeypatch, _TRUST_2)

    await _resolve(AuthContext(tenant_id="t1", agent_id="reviewer-bot"))

    assert _filed_as(sc) == "reviewer-bot"


# ── M-30: a slashed document id is addressable by id ─────────────────────


async def test_m30_a_document_with_a_slash_in_its_id_is_read_and_deleted(client):
    tenant_id, headers = get_test_auth()
    collection = f"runbooks-{uid()}"
    doc_id = "db/failover"
    written = await client.post(
        "/api/v1/documents",
        json={
            "tenant_id": tenant_id,
            "collection": collection,
            "doc_id": doc_id,
            "data": {"step": "promote the replica"},
        },
        headers=headers,
    )
    assert written.status_code == 200, written.text
    url = f"/api/v1/documents/{doc_id}?tenant_id={tenant_id}&collection={collection}"

    got = await client.get(url, headers=headers)
    assert got.status_code == 200, got.text
    assert got.json()["doc_id"] == doc_id

    gone = await client.delete(url, headers=headers)
    assert gone.status_code == 204, gone.text
    assert (await client.get(url, headers=headers)).status_code == 404


async def test_m30_a_by_id_read_without_its_collection_says_what_is_missing(client):
    """The guessed ``/documents/{collection}/{doc_id}`` shape now reaches the
    by-id route, so it is told which query parameter it lacks, not 404."""
    tenant_id, headers = get_test_auth()

    resp = await client.get(
        f"/api/v1/documents/skills/my-doc?tenant_id={tenant_id}", headers=headers
    )

    assert resp.status_code == 422, resp.text
    assert "collection" in resp.text


# ── L-20: the fleet PATCH body is typed ──────────────────────────────────


@pytest.mark.parametrize("fleet_id", [123, "", None, ["f1"]])
async def test_l20_a_fleet_that_is_not_a_name_is_a_422(client, as_auth, fleet_id):
    """A non-string reached storage and came back as a retryable 503."""
    tenant = f"tenant-{uid()}"
    as_auth(tenant_id=tenant)

    resp = await client.patch(
        f"/api/v1/agents/a1/fleet?tenant_id={tenant}", json={"fleet_id": fleet_id}
    )

    assert resp.status_code == 422, resp.text


# ── L-21: an agent delete is audited once it has happened ────────────────


@pytest.mark.parametrize(
    "outcome",
    [AsyncMock(return_value=False), AsyncMock(side_effect=RuntimeError("down"))],
    ids=["no row", "storage error"],
)
async def test_l21_a_failed_agent_delete_is_not_audited(monkeypatch, outcome):
    monkeypatch.setattr(
        agents,
        "lookup_agent",
        AsyncMock(return_value={"agent_id": "a1", "fleet_id": None}),
    )
    sc = MagicMock()
    sc.delete_agent = outcome
    monkeypatch.setattr(agents, "get_storage_client", lambda: sc)
    audit = AsyncMock()
    monkeypatch.setattr(agents, "log_action", audit)

    with pytest.raises((HTTPException, RuntimeError)):
        await agents.delete_agent(
            "a1", tenant_id="t1", auth=AuthContext(tenant_id="t1")
        )

    audit.assert_not_awaited()


# ── L-22: the entity list pages ──────────────────────────────────────────


async def test_l22_the_entity_list_forwards_its_offset(client, as_auth, monkeypatch):
    sc = MagicMock()
    sc.list_entities = AsyncMock(return_value=[])
    monkeypatch.setattr(entities, "get_storage_client", lambda: sc)
    tenant = f"tenant-{uid()}"
    as_auth(tenant_id=tenant, readable_tenant_ids=[tenant])

    resp = await client.get(f"/api/v1/entities?tenant_id={tenant}&limit=10&offset=500")

    assert resp.status_code == 200, resp.text
    assert sc.list_entities.await_args.kwargs.get("offset") == 500


# ── L-23: an admin list or stats call names its tenant ───────────────────


@pytest.mark.parametrize("path", ["/api/v1/memories", "/api/v1/memories/stats"])
async def test_l23_an_admin_naming_no_tenant_is_told_to(
    client, as_auth, monkeypatch, path
):
    """Storage needs a tenant for both; forwarding none was a 422 there and a
    500 here, where ``/memories/count`` already answers 400.

    Standalone fills a missing ``tenant_id`` in before routing, so that is
    switched off here: the gap is on the gateway path, where nothing does.
    """
    from core_api.middleware import standalone_tenant

    monkeypatch.setattr(standalone_tenant, "get_standalone_tenant_id", lambda: None)
    as_auth(tenant_id=None, is_admin=True)

    resp = await client.get(path)

    assert resp.status_code == 400, resp.text
    assert "tenant_id" in resp.text
