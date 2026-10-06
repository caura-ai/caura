"""Agent-credential gates that sibling write/read routes already applied.

Each route here held an agent-scoped credential to less than its neighbours:

- ``POST /documents`` — no fleet policy (``enforce_fleet_write``), and an
  upsert could replace or ``force``-blank a document another agent authored,
  while ``DELETE /documents/{doc_id}`` needs trust >= 3.
- ``POST /entities/upsert`` / ``POST /relations/upsert`` — no fleet policy;
  then (M-84) no trust-0 refusal, an omitted fleet written fleet-less, and a
  relation reaching another fleet's entities, evidence or edge by id.
- ``POST /ingest/commit`` — none of the ``/memories/bulk`` identity chain
  (binding, registration, fleet policy).
- ``POST /ingest/undo/{run_id}`` — no ``enforce_delete``.
- ``GET /memories`` / ``GET /memories/stats`` — ``include_deleted`` honoured
  for any credential, while MCP ``caura_list`` / ``caura_stats`` restrict it to
  trust >= 3.
- ``PATCH /agents/{id}/trust`` / ``/fleet`` — no audit row.
- ``POST /interview/submit`` — caller-named ``agent_id`` / ``node_id`` and an
  unbounded ``cursor_to``.
- ``get_or_create_agent`` — first-touch registration from read paths ignored
  the tenant's ``require_agent_approval``.
- ``POST /documents`` and ``POST /interview/submit`` — an agent awaiting
  approval (trust 0) wrote, where ``POST /memories`` refuses it (M-89).
- ``POST /documents`` / ``DELETE /documents/{doc_id}`` — reached the interview
  service's own collections (M-86).

Tenant credentials (no agent identity) keep their tenant-wide authority on
every one of these routes; each group pins that alongside the new gate.

Agent rows are seeded through the storage client (``sc``) so the in-process
storage app sees them; ``as_auth`` stands in for the gateway's header-trust
path, as in ``test_route_authz_gaps.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

import core_api.services.interview_service as interview_service
from core_api import errors
from core_api.routes import agents as agents_routes
from core_api.routes import memories as memories_routes
from core_api.services.agent_service import get_or_create_agent
from core_api.services.organization_settings import invalidate_cache
from tests.conftest import new_tenant_id

pytestmark = pytest.mark.asyncio


@pytest.fixture
def as_auth(monkeypatch):
    """Override get_auth_context with a controlled AuthContext."""
    from core_api.app import app
    from core_api.auth import AuthContext, get_auth_context
    from core_api.tenant_context import set_current_tenant

    def _install(tenant_id: str, agent_id: str | None = None, **kwargs):
        async def _dep():
            set_current_tenant(tenant_id)
            return AuthContext(
                tenant_id=tenant_id,
                agent_id=agent_id,
                readable_tenant_ids=[tenant_id],
                **kwargs,
            )

        app.dependency_overrides[get_auth_context] = _dep

    yield _install
    app.dependency_overrides.pop(get_auth_context, None)


def _uid() -> str:
    return uuid.uuid4().hex[:8]


async def _seed_agent(
    sc, tenant_id: str, agent_id: str, trust_level: int, fleet_id: str | None = None
):
    await sc.create_or_update_agent(
        {
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "trust_level": trust_level,
            "fleet_id": fleet_id,
        }
    )


async def _write_doc(client, tenant_id: str, doc_id: str, data: dict, **extra):
    return await client.post(
        "/api/v1/documents",
        json={
            "tenant_id": tenant_id,
            "collection": "runbooks",
            "doc_id": doc_id,
            "data": data,
            **extra,
        },
    )


async def _stored_doc(sc, tenant_id: str, doc_id: str) -> dict | None:
    return await sc.get_document(
        tenant_id=tenant_id, collection="runbooks", doc_id=doc_id, read=False
    )


# ---------------------------------------------------------------------------
# POST /documents — overwrite and force need delete-level trust
# ---------------------------------------------------------------------------


async def test_agent_cannot_overwrite_a_document_another_agent_wrote(
    client, as_auth, sc
):
    tenant = new_tenant_id()
    await _seed_agent(sc, tenant, "author", 1, "f1")
    await _seed_agent(sc, tenant, "low", 1, "f2")
    doc_id = f"doc-{_uid()}"
    as_auth(tenant, agent_id="author")
    assert (
        await _write_doc(client, tenant, doc_id, {"steps": "the real runbook"})
    ).status_code == 200

    as_auth(tenant, agent_id="low")
    resp = await _write_doc(client, tenant, doc_id, {"steps": "replaced"})

    assert resp.status_code == 403, resp.text
    assert (await _stored_doc(sc, tenant, doc_id))["data"] == {
        "steps": "the real runbook"
    }


async def test_fleet_peer_may_update_a_document_its_fleet_shares(client, as_auth, sc):
    """Agents of one fleet share documents (task checklists, runbooks): a
    trust-1 peer may update one another agent of its own fleet wrote."""
    tenant = new_tenant_id()
    await _seed_agent(sc, tenant, "author", 1, "f1")
    await _seed_agent(sc, tenant, "peer", 1, "f1")
    doc_id = f"doc-{_uid()}"
    as_auth(tenant, agent_id="author")
    assert (
        await _write_doc(client, tenant, doc_id, {"steps": "v1"})
    ).status_code == 200

    as_auth(tenant, agent_id="peer")
    resp = await _write_doc(client, tenant, doc_id, {"steps": "v2"})

    assert resp.status_code == 200, resp.text
    assert (await _stored_doc(sc, tenant, doc_id))["data"] == {"steps": "v2"}


async def test_agent_may_still_overwrite_an_unowned_document(client, as_auth, sc):
    """A document with no recorded author (legacy rows, MCP writes from before
    authors were recorded, system writes) is unowned: a trust-1 agent may
    still update it, so shared documents such as task checklists keep
    working. Only a DIFFERENT recorded author raises the bar."""
    tenant = new_tenant_id()
    await _seed_agent(sc, tenant, "low", 1, "f1")
    doc_id = f"doc-{_uid()}"
    await sc.upsert_document(
        {
            "tenant_id": tenant,
            "collection": "runbooks",
            "doc_id": doc_id,
            "data": {"v": 1},
        }
    )

    as_auth(tenant, agent_id="low")
    resp = await _write_doc(client, tenant, doc_id, {"v": 2})

    assert resp.status_code == 200, resp.text
    assert (await _stored_doc(sc, tenant, doc_id))["data"] == {"v": 2}


async def test_agent_cannot_force_blank_even_its_own_document_below_trust_3(
    client, as_auth, sc
):
    tenant = new_tenant_id()
    await _seed_agent(sc, tenant, "low", 1, "f1")
    doc_id = f"doc-{_uid()}"
    as_auth(tenant, agent_id="low")
    assert (
        await _write_doc(client, tenant, doc_id, {"steps": "x" * 400})
    ).status_code == 200

    resp = await _write_doc(client, tenant, doc_id, {}, force=True)

    assert resp.status_code == 403, resp.text
    assert (await _stored_doc(sc, tenant, doc_id))["data"] == {"steps": "x" * 400}


async def test_agent_can_still_update_its_own_document(client, as_auth, sc):
    tenant = new_tenant_id()
    await _seed_agent(sc, tenant, "low", 1, "f1")
    doc_id = f"doc-{_uid()}"
    as_auth(tenant, agent_id="low")
    assert (await _write_doc(client, tenant, doc_id, {"v": 1})).status_code == 200
    resp = await _write_doc(client, tenant, doc_id, {"v": 2})
    assert resp.status_code == 200, resp.text
    assert (await _stored_doc(sc, tenant, doc_id))["data"] == {"v": 2}


async def test_trust_3_agent_and_tenant_key_may_still_overwrite(client, as_auth, sc):
    tenant = new_tenant_id()
    await _seed_agent(sc, tenant, "author", 1, "f1")
    await _seed_agent(sc, tenant, "boss", 3, "f1")
    doc_id = f"doc-{_uid()}"
    as_auth(tenant, agent_id="author")
    assert (await _write_doc(client, tenant, doc_id, {"v": 1})).status_code == 200

    as_auth(tenant, agent_id="boss")
    assert (await _write_doc(client, tenant, doc_id, {"v": 2})).status_code == 200
    as_auth(tenant)  # tenant key: no agent identity, tenant-wide authority
    assert (await _write_doc(client, tenant, doc_id, {"v": 3})).status_code == 200
    assert (await _write_doc(client, tenant, doc_id, {}, force=True)).status_code == 200


# ---------------------------------------------------------------------------
# POST /documents — fleet policy
# ---------------------------------------------------------------------------


async def test_agent_document_write_into_another_fleet_is_refused(client, as_auth, sc):
    tenant = new_tenant_id()
    await _seed_agent(sc, tenant, "low", 1, "home-fleet")
    as_auth(tenant, agent_id="low")
    doc_id = f"doc-{_uid()}"

    resp = await _write_doc(client, tenant, doc_id, {"v": 1}, fleet_id="other-fleet")

    assert resp.status_code == 403, resp.text
    assert await _stored_doc(sc, tenant, doc_id) is None


async def test_agent_document_write_without_fleet_lands_in_its_home_fleet(
    client, as_auth, sc
):
    tenant = new_tenant_id()
    await _seed_agent(sc, tenant, "low", 1, "home-fleet")
    as_auth(tenant, agent_id="low")
    doc_id = f"doc-{_uid()}"

    resp = await _write_doc(client, tenant, doc_id, {"v": 1})

    assert resp.status_code == 200, resp.text
    assert (await _stored_doc(sc, tenant, doc_id))["fleet_id"] == "home-fleet"


# ---------------------------------------------------------------------------
# POST /entities/upsert, POST /relations/upsert — fleet policy
# ---------------------------------------------------------------------------


async def _entity(client, tenant: str, name: str, fleet_id: str | None):
    return await client.post(
        "/api/v1/entities/upsert",
        json={
            "tenant_id": tenant,
            "fleet_id": fleet_id,
            "entity_type": "service",
            "canonical_name": name,
        },
    )


async def test_agent_entity_upsert_into_another_fleet_is_refused(client, as_auth, sc):
    tenant = new_tenant_id()
    await _seed_agent(sc, tenant, "low", 1, "home-fleet")
    as_auth(tenant, agent_id="low")

    assert (
        await _entity(client, tenant, f"own-{_uid()}", "home-fleet")
    ).status_code == 200
    resp = await _entity(client, tenant, f"foreign-{_uid()}", "other-fleet")
    assert resp.status_code == 403, resp.text


async def test_agent_relation_upsert_into_another_fleet_is_refused(client, as_auth, sc):
    tenant = new_tenant_id()
    await _seed_agent(sc, tenant, "low", 1, "home-fleet")
    as_auth(tenant)  # the entities exist, created by a tenant key
    a = (await _entity(client, tenant, f"a-{_uid()}", "other-fleet")).json()["id"]
    b = (await _entity(client, tenant, f"b-{_uid()}", "other-fleet")).json()["id"]

    as_auth(tenant, agent_id="low")
    resp = await client.post(
        "/api/v1/relations/upsert",
        json={
            "tenant_id": tenant,
            "fleet_id": "other-fleet",
            "from_entity_id": a,
            "relation_type": "depends_on",
            "to_entity_id": b,
        },
    )
    assert resp.status_code == 403, resp.text


# M-84: the memory write's other gates, and the fleets a relation reaches
# through its ids rather than its body's label.


async def _relation(client, tenant: str, a: str, b: str, **extra):
    return await client.post(
        "/api/v1/relations/upsert",
        json={
            "tenant_id": tenant,
            "from_entity_id": a,
            "relation_type": "depends_on",
            "to_entity_id": b,
            **extra,
        },
    )


async def _entities(client, as_auth, tenant: str, *fleets: str | None) -> list[str]:
    """One entity per fleet in ``fleets``, created by a tenant key."""
    as_auth(tenant)
    return [
        (await _entity(client, tenant, f"e-{_uid()}", fleet)).json()["id"]
        for fleet in fleets
    ]


async def _memory(sc, tenant: str, fleet_id: str | None) -> str:
    row = await sc.create_memory(
        {
            "tenant_id": tenant,
            "fleet_id": fleet_id,
            "agent_id": "author",
            "memory_type": "fact",
            "content": f"evidence {_uid()}",
            "status": "active",
            "visibility": "scope_team",
        }
    )
    return str(row["id"])


async def test_agent_awaiting_approval_cannot_write_the_graph(client, as_auth, sc):
    """``POST /memories`` refuses an agent at trust 0; the graph routes did not."""
    tenant = new_tenant_id()
    await _seed_agent(sc, tenant, "pending", 0, "home-fleet")
    a, b = await _entities(client, as_auth, tenant, "home-fleet", "home-fleet")

    as_auth(tenant, agent_id="pending")
    entity = await _entity(client, tenant, f"x-{_uid()}", "home-fleet")
    assert entity.status_code == 403, entity.text
    relation = await _relation(client, tenant, a, b, fleet_id="home-fleet")
    assert relation.status_code == 403, relation.text


async def test_agent_graph_write_without_fleet_lands_in_its_home_fleet(
    client, as_auth, sc
):
    """As on ``POST /memories``. A fleet-less node or edge is read by every
    fleet, so an omitted ``fleet_id`` reached all of them."""
    tenant = new_tenant_id()
    await _seed_agent(sc, tenant, "low", 1, "home-fleet")
    a, b = await _entities(client, as_auth, tenant, None, None)

    as_auth(tenant, agent_id="low")
    entity = await _entity(client, tenant, f"x-{_uid()}", None)
    assert entity.status_code == 200, entity.text
    assert entity.json()["fleet_id"] == "home-fleet"
    relation = await _relation(client, tenant, a, b)
    assert relation.status_code == 200, relation.text
    assert relation.json()["fleet_id"] == "home-fleet"


@pytest.mark.parametrize("foreign_end", ["from", "to"])
async def test_agent_cannot_link_another_fleets_entity(
    client, as_auth, sc, foreign_end
):
    """The body names the agent's own fleet; an endpoint is another fleet's."""
    tenant = new_tenant_id()
    await _seed_agent(sc, tenant, "low", 1, "home-fleet")
    own, other = await _entities(client, as_auth, tenant, "home-fleet", "other-fleet")
    a, b = (other, own) if foreign_end == "from" else (own, other)

    as_auth(tenant, agent_id="low")
    resp = await _relation(client, tenant, a, b, fleet_id="home-fleet")
    assert resp.status_code == 403, resp.text


async def test_agent_cannot_cite_another_fleets_memory_as_evidence(client, as_auth, sc):
    tenant = new_tenant_id()
    await _seed_agent(sc, tenant, "low", 1, "home-fleet")
    a, b = await _entities(client, as_auth, tenant, "home-fleet", None)
    evidence = await _memory(sc, tenant, "other-fleet")

    as_auth(tenant, agent_id="low")
    resp = await _relation(client, tenant, a, b, evidence_memory_id=evidence)
    assert resp.status_code == 403, resp.text


@pytest.mark.parametrize("edge_fleet", ["other-fleet", None])
async def test_agent_cannot_rewrite_an_edge_outside_its_fleet(
    client, as_auth, sc, edge_fleet
):
    """A relation's natural key leaves its fleet out, so an upsert of an existing
    edge rewrote that edge's weight and evidence wherever it lived. A fleet-less
    edge is read by every fleet's graph expansion."""
    tenant = new_tenant_id()
    await _seed_agent(sc, tenant, "low", 1, "home-fleet")
    a, b = await _entities(client, as_auth, tenant, None, None)
    first = await _relation(client, tenant, a, b, fleet_id=edge_fleet, weight=0.9)
    assert first.status_code == 200, first.text

    as_auth(tenant, agent_id="low")
    resp = await _relation(client, tenant, a, b, weight=0.1)
    assert resp.status_code == 403, resp.text
    [edge] = await sc.get_outgoing_relations(a, tenant)
    assert edge["relation"]["weight"] == pytest.approx(0.9)


async def test_agent_still_links_and_rewrites_within_its_fleet(client, as_auth, sc):
    """Control: its own fleet's entities, fleet-less ones and its own fleet's
    evidence, and a second upsert of the edge it made."""
    tenant = new_tenant_id()
    await _seed_agent(sc, tenant, "low", 1, "home-fleet")
    own, shared = await _entities(client, as_auth, tenant, "home-fleet", None)
    evidence = await _memory(sc, tenant, "home-fleet")

    as_auth(tenant, agent_id="low")
    created = await _relation(
        client, tenant, own, shared, evidence_memory_id=evidence, weight=0.4
    )
    assert created.status_code == 200, created.text
    again = await _relation(client, tenant, own, shared, weight=0.6)
    assert again.status_code == 200, again.text
    assert again.json()["weight"] == pytest.approx(0.6)


async def test_trust_3_agent_still_links_across_fleets(client, as_auth, sc):
    tenant = new_tenant_id()
    await _seed_agent(sc, tenant, "boss", 3, "home-fleet")
    a, b = await _entities(client, as_auth, tenant, "other-fleet", "other-fleet")

    as_auth(tenant, agent_id="boss")
    resp = await _relation(client, tenant, a, b, fleet_id="other-fleet")
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# POST /ingest/commit — the /memories/bulk identity chain
# ---------------------------------------------------------------------------


@pytest.fixture
def captured_ingest(monkeypatch):
    captured: dict = {}

    async def _ingest(body):
        captured["agent_id"] = body.agent_id
        captured["fleet_id"] = body.fleet_id
        return {"committed": len(body.facts)}

    monkeypatch.setattr(memories_routes, "ingest_commit", _ingest)
    return captured


def _commit_body(tenant: str, **kw) -> dict:
    return {
        "tenant_id": tenant,
        "facts": [{"content": "a durable fact", "suggested_type": "fact"}],
        **kw,
    }


async def test_ingest_commit_writes_as_the_verified_agent_not_a_named_peer(
    client, as_auth, sc, captured_ingest
):
    tenant = new_tenant_id()
    await _seed_agent(sc, tenant, "low", 1, "home-fleet")
    await _seed_agent(sc, tenant, "peer", 1, "home-fleet")
    as_auth(tenant, agent_id="low")

    resp = await client.post(
        "/api/v1/ingest/commit", json=_commit_body(tenant, agent_id="peer")
    )

    assert resp.status_code == 200, resp.text
    assert captured_ingest["agent_id"] == "low"
    assert captured_ingest["fleet_id"] == "home-fleet"


async def test_ingest_commit_into_another_fleet_is_refused(
    client, as_auth, sc, captured_ingest
):
    tenant = new_tenant_id()
    await _seed_agent(sc, tenant, "low", 1, "home-fleet")
    as_auth(tenant, agent_id="low")

    resp = await client.post(
        "/api/v1/ingest/commit", json=_commit_body(tenant, fleet_id="other-fleet")
    )

    assert resp.status_code == 403, resp.text
    assert captured_ingest == {}


async def test_ingest_commit_registers_the_writing_agent(
    client, as_auth, sc, captured_ingest
):
    tenant = new_tenant_id()
    as_auth(tenant)  # tenant key, default "ingest-agent" identity

    resp = await client.post("/api/v1/ingest/commit", json=_commit_body(tenant))

    assert resp.status_code == 200, resp.text
    assert captured_ingest["agent_id"] == "ingest-agent"
    assert await sc.get_agent("ingest-agent", tenant, read=False) is not None


# ---------------------------------------------------------------------------
# POST /ingest/undo/{run_id} — delete-level trust
# ---------------------------------------------------------------------------


async def test_ingest_undo_needs_trust_3_for_an_agent_credential(client, as_auth, sc):
    tenant = new_tenant_id()
    await _seed_agent(sc, tenant, "low", 1)
    await _seed_agent(sc, tenant, "boss", 3)
    run_id = f"run-{_uid()}"

    as_auth(tenant, agent_id="low")
    resp = await client.post(f"/api/v1/ingest/undo/{run_id}?tenant_id={tenant}")
    assert resp.status_code == 403, resp.text

    as_auth(tenant, agent_id="boss")
    assert (
        await client.post(f"/api/v1/ingest/undo/{run_id}?tenant_id={tenant}")
    ).status_code == 200
    as_auth(tenant)
    assert (
        await client.post(f"/api/v1/ingest/undo/{run_id}?tenant_id={tenant}")
    ).status_code == 200


# ---------------------------------------------------------------------------
# GET /memories, GET /memories/stats — include_deleted is trust-3 for agents
# ---------------------------------------------------------------------------


async def _deleted_memory(client, as_auth, tenant: str, agent: str) -> str:
    as_auth(tenant, agent_id=agent)
    resp = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant,
            "content": f"retracted fact {_uid()}",
            "memory_type": "fact",
        },
    )
    assert resp.status_code in (200, 201), resp.text
    memory_id = resp.json()["id"]
    as_auth(tenant)
    resp = await client.delete(f"/api/v1/memories/{memory_id}?tenant_id={tenant}")
    assert resp.status_code in (200, 204), resp.text
    return memory_id


async def test_low_trust_agent_does_not_see_soft_deleted_rows(client, as_auth, sc):
    tenant = new_tenant_id()
    await _seed_agent(sc, tenant, "low", 1, "home-fleet")
    memory_id = await _deleted_memory(client, as_auth, tenant, "low")

    as_auth(tenant, agent_id="low")
    listing = await client.get(
        f"/api/v1/memories?tenant_id={tenant}&include_deleted=true"
    )
    assert listing.status_code == 200, listing.text
    assert memory_id not in {m["id"] for m in listing.json()["items"]}
    stats = await client.get(
        f"/api/v1/memories/stats?tenant_id={tenant}&include_deleted=true"
    )
    assert stats.status_code == 200, stats.text
    assert not stats.json().get("deleted")

    # A tenant credential keeps the flag as sent.
    as_auth(tenant)
    listing = await client.get(
        f"/api/v1/memories?tenant_id={tenant}&include_deleted=true"
    )
    assert memory_id in {m["id"] for m in listing.json()["items"]}


async def test_trust_3_agent_still_sees_soft_deleted_rows(client, as_auth, sc):
    tenant = new_tenant_id()
    await _seed_agent(sc, tenant, "boss", 3, "home-fleet")
    memory_id = await _deleted_memory(client, as_auth, tenant, "boss")

    as_auth(tenant, agent_id="boss")
    listing = await client.get(
        f"/api/v1/memories?tenant_id={tenant}&include_deleted=true"
    )
    assert memory_id in {m["id"] for m in listing.json()["items"]}


# ---------------------------------------------------------------------------
# PATCH /agents/{id}/trust, PATCH /agents/{id}/fleet — audited
# ---------------------------------------------------------------------------


async def test_trust_and_fleet_changes_write_an_audit_row(
    client, as_auth, sc, monkeypatch
):
    tenant = new_tenant_id()
    await _seed_agent(sc, tenant, "worker", 1, "f1")
    audit = AsyncMock()
    monkeypatch.setattr(agents_routes, "log_action", audit)
    as_auth(tenant, user_id="user-1")

    resp = await client.patch(
        f"/api/v1/agents/worker/trust?tenant_id={tenant}", json={"trust_level": 3}
    )
    assert resp.status_code == 200, resp.text
    resp = await client.patch(
        f"/api/v1/agents/worker/fleet?tenant_id={tenant}", json={"fleet_id": "f2"}
    )
    assert resp.status_code == 200, resp.text

    calls = {c.kwargs["action"]: c.kwargs for c in audit.await_args_list}
    trust = calls["agent_trust_update"]
    assert trust["resource_type"] == "agent"
    assert trust["detail"]["old_trust_level"] == 1
    assert trust["detail"]["new_trust_level"] == 3
    assert trust["detail"]["user_id"] == "user-1"
    fleet = calls["agent_fleet_update"]
    assert fleet["detail"]["old_fleet_id"] == "f1"
    assert fleet["detail"]["new_fleet_id"] == "f2"


# ---------------------------------------------------------------------------
# get_or_create_agent — first touch honours require_agent_approval
# ---------------------------------------------------------------------------


async def _require_approval(client, as_auth, tenant: str) -> None:
    as_auth(tenant)
    resp = await client.put(
        f"/api/v1/settings?tenant_id={tenant}",
        json={"agents": {"require_agent_approval": True}},
    )
    assert resp.status_code == 200, resp.text
    invalidate_cache(tenant)


async def test_search_first_touch_registers_at_trust_0_under_approval(
    client, as_auth, sc
):
    tenant = new_tenant_id()
    await _require_approval(client, as_auth, tenant)

    as_auth(tenant, agent_id="newbie")
    resp = await client.post(
        "/api/v1/search", json={"tenant_id": tenant, "query": "anything"}
    )
    assert resp.status_code == 200, resp.text

    agent = await sc.get_agent("newbie", tenant, read=False)
    assert agent is not None
    assert agent["trust_level"] == 0


async def test_default_first_touch_honours_the_tenant_setting(client, as_auth, sc):
    gated = new_tenant_id()
    await _require_approval(client, as_auth, gated)
    open_tenant = new_tenant_id()

    assert (await get_or_create_agent(gated, "via-tune"))["trust_level"] == 0
    # No approval setting: unchanged, DEFAULT_TRUST_LEVEL.
    assert (await get_or_create_agent(open_tenant, "via-tune"))["trust_level"] == 1
    # An explicit opt-out (REST bulk) still wins.
    assert (await get_or_create_agent(gated, "via-bulk", require_approval=False))[
        "trust_level"
    ] == 1


# ---------------------------------------------------------------------------
# POST /interview/submit — identity, node, cursor bound
# ---------------------------------------------------------------------------


def _events(n: int = 3, start_seq: int = 0) -> list[dict]:
    base = datetime(2026, 7, 16, 8, 0, tzinfo=UTC)
    return [
        {
            "seq": start_seq + i,
            "ts": (base + timedelta(minutes=i)).isoformat(),
            "session_id": "sess-1",
            "role": "assistant",
            "kind": "message",
            "content": f"Worked on step {i}.",
        }
        for i in range(n)
    ]


@pytest.fixture
async def interview_tenant(client, as_auth, monkeypatch):
    """A tenant with the interviewer on, one registered node, async submit on."""
    import core_api.routes.interview as interview_route

    monkeypatch.setattr(interview_route.app_settings, "interview_async_submit", True)
    # Synthesis runs fire-and-forget after the 200; keep it off the network.
    monkeypatch.setattr(interview_route, "_bounded_process", AsyncMock())
    tenant = new_tenant_id()
    as_auth(tenant)
    resp = await client.put(
        f"/api/v1/settings?tenant_id={tenant}", json={"interviewer": {"enabled": True}}
    )
    assert resp.status_code == 200, resp.text
    resp = await client.post(
        "/api/v1/fleet/heartbeat",
        json={"tenant_id": tenant, "node_name": f"node-{_uid()}"},
    )
    assert resp.status_code == 200, resp.text
    return tenant, resp.json()["node_id"]


def _submit(tenant: str, node_id: str, agent_id: str, **kw) -> dict:
    events = kw.pop("events", _events())
    return {
        "tenant_id": tenant,
        "node_id": node_id,
        "agent_id": agent_id,
        "command_id": "cmd-1",
        "cursor_from": events[0]["seq"],
        "cursor_to": events[-1]["seq"],
        "events": events,
        **kw,
    }


async def _queue_request(client, tenant: str, node_id: str) -> str:
    """Queue an ``interview_request`` for the node, as the scheduler does."""
    resp = await client.post(
        "/api/v1/fleet/commands",
        json={
            "tenant_id": tenant,
            "node_id": node_id,
            "command": "interview_request",
            "payload": {"node_id": node_id},
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _delivered_request(client, as_auth, tenant: str, node_id: str) -> str:
    """An ``interview_request`` the node's heartbeat has collected (and acked)."""
    as_auth(tenant)
    command_id = await _queue_request(client, tenant, node_id)
    nodes = await client.get(f"/api/v1/fleet/nodes?tenant_id={tenant}")
    name = next(n["node_name"] for n in nodes.json() if n["node_id"] == node_id)
    beat = await client.post(
        "/api/v1/fleet/heartbeat", json={"tenant_id": tenant, "node_name": name}
    )
    assert command_id in {c["id"] for c in beat.json()["commands"]}, beat.text
    return command_id


async def test_interview_submit_for_an_unknown_node_is_refused(
    client, as_auth, interview_tenant
):
    tenant, _node = interview_tenant
    stranger = str(uuid.uuid4())
    resp = await client.post(
        "/api/v1/interview/submit", json=_submit(tenant, stranger, "worker")
    )
    assert resp.status_code == 404, resp.text
    assert await interview_service.read_watermark(tenant, stranger) == -1


async def test_interview_submit_cannot_jump_the_watermark(
    client, as_auth, interview_tenant
):
    tenant, node_id = interview_tenant
    far = _events(1, start_seq=10**9)
    resp = await client.post(
        "/api/v1/interview/submit", json=_submit(tenant, node_id, "worker", events=far)
    )
    assert resp.status_code == 422, resp.text
    assert await interview_service.read_watermark(tenant, node_id) == -1


async def test_interview_submit_attributes_to_the_verified_agent(
    client, as_auth, sc, interview_tenant
):
    tenant, node_id = interview_tenant
    await _seed_agent(sc, tenant, "low", 1)
    command_id = await _delivered_request(client, as_auth, tenant, node_id)
    as_auth(tenant, agent_id="low")

    resp = await client.post(
        "/api/v1/interview/submit",
        json=_submit(tenant, node_id, "peer", command_id=command_id),
    )
    assert resp.status_code == 200, resp.text

    job = await sc.get_document(
        tenant,
        interview_service.JOBS_COLLECTION,
        interview_service.interview_job_doc_id(node_id, 0, 2),
        read=False,
    )
    assert job["data"]["agent_id"] == "low"
    assert await interview_service.read_watermark(tenant, node_id) == 2


async def test_an_agent_cannot_submit_a_fleet_node_window_uninvited(
    client, as_auth, sc, interview_tenant
):
    """THE GAP (M-86): any agent credential could write any node's window.

    It named the node and any ``command_id``, and the window advanced that
    node's watermark and took its job doc. A fleet node's window now has to
    cite the ``interview_request`` the scheduler sent that node.
    """
    tenant, node_id = interview_tenant
    await _seed_agent(sc, tenant, "low", 1)
    as_auth(tenant, agent_id="low")

    resp = await client.post(
        "/api/v1/interview/submit", json=_submit(tenant, node_id, "low")
    )

    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == errors.AUTH_INTERVIEW_REQUEST_REQUIRED
    assert await interview_service.read_watermark(tenant, node_id) == -1


async def test_an_install_cannot_submit_a_fleet_node_window_uninvited(
    client, as_auth, interview_tenant
):
    tenant, node_id = interview_tenant
    as_auth(tenant, is_install_credential=True, install_uuid=f"install-{_uid()}")

    resp = await client.post(
        "/api/v1/interview/submit", json=_submit(tenant, node_id, "laptop")
    )

    assert resp.status_code == 403, resp.text
    assert await interview_service.read_watermark(tenant, node_id) == -1


async def test_a_queued_but_undelivered_request_admits_nothing(
    client, as_auth, sc, interview_tenant
):
    tenant, node_id = interview_tenant
    await _seed_agent(sc, tenant, "low", 1)
    command_id = await _queue_request(client, tenant, node_id)
    as_auth(tenant, agent_id="low")

    resp = await client.post(
        "/api/v1/interview/submit",
        json=_submit(tenant, node_id, "low", command_id=command_id),
    )

    assert resp.status_code == 403, resp.text
    assert await interview_service.read_watermark(tenant, node_id) == -1


async def test_a_request_admits_one_window(client, as_auth, sc, interview_tenant):
    """Once used, a request's id is no secret: the job doc and the watermark
    record it. So a second window citing it is refused."""
    tenant, node_id = interview_tenant
    await _seed_agent(sc, tenant, "low", 1)
    command_id = await _delivered_request(client, as_auth, tenant, node_id)
    as_auth(tenant, agent_id="low")
    first = _submit(tenant, node_id, "low", command_id=command_id)
    resp = await client.post("/api/v1/interview/submit", json=first)
    assert resp.status_code == 200, resp.text

    again = _submit(
        tenant, node_id, "low", command_id=command_id, events=_events(start_seq=3)
    )
    resp = await client.post("/api/v1/interview/submit", json=again)

    assert resp.status_code == 403, resp.text
    assert await interview_service.read_watermark(tenant, node_id) == 2


async def test_a_request_for_another_node_admits_nothing(
    client, as_auth, sc, interview_tenant
):
    tenant, node_id = interview_tenant
    await _seed_agent(sc, tenant, "low", 1)
    other = await client.post(
        "/api/v1/fleet/heartbeat",
        json={"tenant_id": tenant, "node_name": f"node-{_uid()}"},
    )
    other_id = other.json()["node_id"]
    command_id = await _delivered_request(client, as_auth, tenant, other_id)
    as_auth(tenant, agent_id="low")

    resp = await client.post(
        "/api/v1/interview/submit",
        json=_submit(tenant, node_id, "low", command_id=command_id),
    )

    assert resp.status_code == 403, resp.text
    assert await interview_service.read_watermark(tenant, node_id) == -1


async def test_a_tenant_credential_submits_a_fleet_node_window_directly(
    client, as_auth, interview_tenant
):
    """OVER-REFUSAL GUARD: tenant credentials keep tenant-wide authority."""
    tenant, node_id = interview_tenant
    as_auth(tenant)

    resp = await client.post(
        "/api/v1/interview/submit", json=_submit(tenant, node_id, "worker")
    )

    assert resp.status_code == 200, resp.text


def _adapter_stream() -> str:
    """A ``caura-interviewer`` stream key: ``cc:<machine12>:<session uuid>``."""
    return f"cc:{uuid.uuid4().hex[:12]}:{uuid.uuid4()}"


# One event far ahead of a fresh stream's cursor, yet inside the per-submit cap:
# the jump the stream binding exists to refuse.
_JUMP = _events(1, start_seq=999_999)


async def test_interview_submit_accepts_an_adapter_stream(
    client, as_auth, interview_tenant
):
    # caura-interviewer keys a stream per transcript and registers no fleet
    # node for it, so a fleet-node check on its key refused every window.
    tenant, _node = interview_tenant
    stream = _adapter_stream()
    resp = await client.post(
        "/api/v1/interview/submit", json=_submit(tenant, stream, "worker")
    )
    assert resp.status_code == 200, resp.text
    assert await interview_service.read_watermark(tenant, stream) == 2


async def test_an_agent_cannot_continue_another_agents_adapter_stream(
    client, as_auth, sc, interview_tenant
):
    tenant, _node = interview_tenant
    await _seed_agent(sc, tenant, "owner", 1)
    await _seed_agent(sc, tenant, "peer", 1)
    stream = _adapter_stream()
    as_auth(tenant, agent_id="owner")
    resp = await client.post(
        "/api/v1/interview/submit", json=_submit(tenant, stream, "owner")
    )
    assert resp.status_code == 200, resp.text

    as_auth(tenant, agent_id="peer")
    resp = await client.post(
        "/api/v1/interview/submit", json=_submit(tenant, stream, "peer", events=_JUMP)
    )
    assert resp.status_code == 409, resp.text
    assert await interview_service.read_watermark(tenant, stream) == 2
    job = await sc.get_document(
        tenant,
        interview_service.JOBS_COLLECTION,
        interview_service.interview_job_doc_id(stream, 999_999, 999_999),
        read=False,
    )
    assert job is None


async def test_an_install_cannot_continue_another_installs_adapter_stream(
    client, as_auth, interview_tenant
):
    # The second install names the first one's agent; the broker ownership
    # gate degrades it to its own ``broker:`` identity, which owns no stream.
    tenant, _node = interview_tenant
    stream = _adapter_stream()
    as_auth(tenant, is_install_credential=True, install_uuid=f"install-{_uid()}")
    resp = await client.post(
        "/api/v1/interview/submit", json=_submit(tenant, stream, "laptop")
    )
    assert resp.status_code == 200, resp.text

    as_auth(tenant, is_install_credential=True, install_uuid=f"install-{_uid()}")
    resp = await client.post(
        "/api/v1/interview/submit", json=_submit(tenant, stream, "laptop", events=_JUMP)
    )
    assert resp.status_code == 409, resp.text
    assert await interview_service.read_watermark(tenant, stream) == 2


async def test_an_agent_continues_its_own_adapter_stream(
    client, as_auth, sc, interview_tenant
):
    tenant, _node = interview_tenant
    await _seed_agent(sc, tenant, "owner", 1)
    stream = _adapter_stream()
    as_auth(tenant, agent_id="owner")
    for window in (_events(), _events(start_seq=3)):
        resp = await client.post(
            "/api/v1/interview/submit",
            json=_submit(tenant, stream, "owner", events=window),
        )
        assert resp.status_code == 200, resp.text
    assert await interview_service.read_watermark(tenant, stream) == 5


async def test_a_tenant_credential_continues_any_adapter_stream(
    client, as_auth, sc, interview_tenant
):
    tenant, _node = interview_tenant
    await _seed_agent(sc, tenant, "owner", 1)
    stream = _adapter_stream()
    as_auth(tenant, agent_id="owner")
    resp = await client.post(
        "/api/v1/interview/submit", json=_submit(tenant, stream, "owner")
    )
    assert resp.status_code == 200, resp.text

    as_auth(tenant)
    resp = await client.post(
        "/api/v1/interview/submit",
        json=_submit(tenant, stream, "peer", events=_events(start_seq=3)),
    )
    assert resp.status_code == 200, resp.text
    assert await interview_service.read_watermark(tenant, stream) == 5


# ---------------------------------------------------------------------------
# M-123 — install credentials on PATCH /agents/{id}/tune and POST /documents
# ---------------------------------------------------------------------------
#
# An install (broker) credential carries no agent identity, so
# ``enforce_self_agent`` passes it, and these two routes trusted the agent it
# named. Their MCP twins and the other REST writes hold it to the broker
# ownership boundary: an agent another install owns is not its to act as.
# Tune is stricter: it claims nothing, so an unclaimed agent is refused too.


@pytest.mark.parametrize("owner", ["install-b", None])
async def test_an_install_cannot_tune_an_agent_it_does_not_own(
    client, as_auth, sc, owner
):
    """``None`` is an agent no install has claimed. Tune does not claim it, so
    a lenient gate would leave it tunable by every install in the tenant."""
    tenant = new_tenant_id()
    await get_or_create_agent(tenant, "victim", owner_install_uuid=owner)
    as_auth(tenant, is_install_credential=True, install_uuid="install-a")

    resp = await client.patch(
        f"/api/v1/agents/victim/tune?tenant_id={tenant}", json={"top_k": 3}
    )

    assert resp.status_code == 403, resp.text
    victim = await sc.get_agent("victim", tenant, read=False)
    assert not (victim.get("search_profile") or {}).get("top_k")


async def test_an_install_still_tunes_an_agent_it_owns(client, as_auth, sc):
    tenant = new_tenant_id()
    await get_or_create_agent(tenant, "mine", owner_install_uuid="install-a")
    as_auth(tenant, is_install_credential=True, install_uuid="install-a")

    resp = await client.patch(
        f"/api/v1/agents/mine/tune?tenant_id={tenant}", json={"top_k": 3}
    )

    assert resp.status_code == 200, resp.text
    mine = await sc.get_agent("mine", tenant, read=False)
    assert mine["search_profile"]["top_k"] == 3


@pytest.mark.parametrize(
    ("owner", "author"),
    [("install-b", "broker:install-a"), ("install-a", "named")],
)
async def test_an_install_writes_a_document_only_as_an_agent_it_owns(
    client, as_auth, sc, owner, author
):
    tenant = new_tenant_id()
    await get_or_create_agent(tenant, "named", owner_install_uuid=owner)
    as_auth(tenant, is_install_credential=True, install_uuid="install-a")
    doc_id = f"doc-{_uid()}"

    resp = await _write_doc(client, tenant, doc_id, {"steps": "x"}, agent_id="named")

    assert resp.status_code == 200, resp.text
    assert (await _stored_doc(sc, tenant, doc_id))["agent_id"] == author


# ---------------------------------------------------------------------------
# M-89 — an agent awaiting approval writes no document and no interview window
# ---------------------------------------------------------------------------
#
# ``POST /memories``, ``caura_write`` and STM refuse a trust-0 agent. These two
# doors registered it on first contact and let it write: a document write mints
# a memory from the document, and an interview window's report lands as
# memories attributed to the submitting agent.


async def test_an_agent_awaiting_approval_cannot_write_a_document(client, as_auth, sc):
    tenant = new_tenant_id()
    await _seed_agent(sc, tenant, "pending", 0)
    as_auth(tenant, agent_id="pending")
    doc_id = f"doc-{_uid()}"

    resp = await _write_doc(client, tenant, doc_id, {"steps": "x"})

    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == errors.AUTH_AGENT_TRUST_TOO_LOW
    assert await _stored_doc(sc, tenant, doc_id) is None


async def test_first_contact_under_approval_cannot_write_a_document(
    client, as_auth, sc
):
    tenant = new_tenant_id()
    await _require_approval(client, as_auth, tenant)
    as_auth(tenant, agent_id="newbie")
    doc_id = f"doc-{_uid()}"

    resp = await _write_doc(client, tenant, doc_id, {"steps": "x"})

    assert resp.status_code == 403, resp.text
    assert await _stored_doc(sc, tenant, doc_id) is None
    assert (await sc.get_agent("newbie", tenant, read=False))["trust_level"] == 0


async def test_an_approved_agent_still_writes_a_document(client, as_auth, sc):
    tenant = new_tenant_id()
    await _require_approval(client, as_auth, tenant)
    await _seed_agent(sc, tenant, "approved", 1)
    as_auth(tenant, agent_id="approved")
    doc_id = f"doc-{_uid()}"

    resp = await _write_doc(client, tenant, doc_id, {"steps": "x"})

    assert resp.status_code == 200, resp.text
    assert (await _stored_doc(sc, tenant, doc_id))["agent_id"] == "approved"


async def test_an_agent_awaiting_approval_cannot_submit_an_interview_window(
    client, as_auth, sc, interview_tenant
):
    tenant, _node = interview_tenant
    await _seed_agent(sc, tenant, "pending", 0)
    as_auth(tenant, agent_id="pending")
    stream = _adapter_stream()

    resp = await client.post(
        "/api/v1/interview/submit", json=_submit(tenant, stream, "pending")
    )

    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == errors.AUTH_AGENT_TRUST_TOO_LOW
    assert await interview_service.read_watermark(tenant, stream) == -1


# ---------------------------------------------------------------------------
# M-86 — the interview service's own collections are not public documents
# ---------------------------------------------------------------------------
#
# Both hold author-less server state, and the overwrite gate lets anyone
# replace an author-less document: a watermark moved ahead stops a node's
# interviews, and a planted pending job's events are written as memories under
# the agent it names. Storage refuses a ``_``-prefixed collection to every
# credential; these two predate that convention, so the routes refuse them.

_INTERVIEW_COLLECTIONS = [
    interview_service.WATERMARK_COLLECTION,
    interview_service.JOBS_COLLECTION,
]


async def _seed_interview_doc(sc, tenant: str, collection: str, doc_id: str) -> None:
    await sc.upsert_document(
        {
            "tenant_id": tenant,
            "collection": collection,
            "doc_id": doc_id,
            "data": {"last_seq": 5},
        }
    )


@pytest.mark.parametrize("collection", _INTERVIEW_COLLECTIONS)
@pytest.mark.parametrize("agent_id", [None, "worker"])
async def test_a_document_write_cannot_reach_the_interview_collections(
    client, as_auth, sc, collection, agent_id
):
    tenant = new_tenant_id()
    await _seed_agent(sc, tenant, "worker", 1)
    as_auth(tenant, agent_id=agent_id)
    doc_id = f"doc-{_uid()}"

    resp = await client.post(
        "/api/v1/documents",
        json={
            "tenant_id": tenant,
            "collection": collection,
            "doc_id": doc_id,
            "data": {"last_seq": 10**9},
        },
    )

    assert resp.status_code == 400, resp.text
    assert await sc.get_document(tenant, collection, doc_id, read=False) is None


@pytest.mark.parametrize("collection", _INTERVIEW_COLLECTIONS)
async def test_a_document_delete_cannot_reach_the_interview_collections(
    client, as_auth, sc, collection
):
    tenant = new_tenant_id()
    doc_id = f"doc-{_uid()}"
    await _seed_interview_doc(sc, tenant, collection, doc_id)
    as_auth(tenant)

    resp = await client.delete(
        f"/api/v1/documents/{doc_id}",
        params={"tenant_id": tenant, "collection": collection},
    )

    assert resp.status_code == 400, resp.text
    assert await sc.get_document(tenant, collection, doc_id, read=False) is not None


async def test_the_interview_watermark_stays_readable(client, as_auth, sc):
    """``caura-interviewer`` reads its stream's watermark through this route."""
    tenant = new_tenant_id()
    doc_id = f"doc-{_uid()}"
    collection = interview_service.WATERMARK_COLLECTION
    await _seed_interview_doc(sc, tenant, collection, doc_id)
    as_auth(tenant)

    resp = await client.get(
        f"/api/v1/documents/{doc_id}",
        params={"tenant_id": tenant, "collection": collection},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == {"last_seq": 5}
