"""``GET /api/v1/memories/field`` end to end, through the in-process storage bridge.

The field must place exactly the rows ``GET /memories`` would list to the same
caller: same tenant check, same visibility scoping (``scope_agent`` rows only
for their author, every scope for a person), same strict fleet filter, deleted
rows out, superseded rows in. Plus the shape the map frontend reads.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

from core_api.app import app
from core_api.auth import AuthContext, get_auth_context
from core_api.constants import VECTOR_DIM
from core_api.services.memory_field import field_cache
from core_api.tenant_context import set_current_tenant
from core_storage_api.services.postgres_service import get_session
from tests.conftest import new_tenant_id

pytestmark = pytest.mark.asyncio

OWNER = "field-owner"
PEER = "field-peer"


@pytest.fixture
def as_auth():
    def _install(
        tenant: str, agent_id: str | None = None, *, person: bool = False
    ) -> None:
        async def _dep():
            set_current_tenant(tenant)
            return AuthContext(
                tenant_id=tenant,
                agent_id=agent_id,
                readable_tenant_ids=[tenant],
                is_person=person,
            )

        app.dependency_overrides[get_auth_context] = _dep

    field_cache.clear()
    yield _install
    app.dependency_overrides.pop(get_auth_context, None)
    field_cache.clear()


def _vec(axis: int, jitter: float = 0.0) -> list[float]:
    v = [0.0] * VECTOR_DIM
    v[axis] = 1.0
    v[(axis + 1) % VECTOR_DIM] = jitter
    return v


async def _write(sc, tenant: str, content: str, **kw) -> str:
    row = await sc.create_memory(
        {
            "tenant_id": tenant,
            "agent_id": kw.pop("agent_id", OWNER),
            "memory_type": kw.pop("memory_type", "fact"),
            "content": content,
            "status": "active",
            "visibility": kw.pop("visibility", "scope_team"),
            "embedding": kw.pop("embedding", None),
            **kw,
        }
    )
    return str(row["id"])


async def _sql(stmt: str, **params) -> None:
    async with get_session() as session:
        await session.execute(text(stmt), params)
        await session.commit()


async def _seed(sc) -> dict:
    """Two topics of shared rows, a private row from each agent, a deleted row,
    a superseded pair, a fleet row and a row with no embedding yet."""
    tenant = new_tenant_id()
    ids: dict = {"tenant": tenant, "topic": []}
    for i in range(6):
        ids["topic"].append(
            await _write(
                sc, tenant, f"billing refund note {i}", embedding=_vec(0, i * 0.01)
            )
        )
    for i in range(6):
        ids["topic"].append(
            await _write(
                sc, tenant, f"deploy pipeline note {i}", embedding=_vec(5, i * 0.01)
            )
        )
    ids["own_private"] = await _write(
        sc, tenant, "owner private", visibility="scope_agent", embedding=_vec(9)
    )
    ids["peer_private"] = await _write(
        sc,
        tenant,
        "peer private",
        agent_id=PEER,
        visibility="scope_agent",
        embedding=_vec(9),
    )
    ids["deleted"] = await _write(sc, tenant, "gone", embedding=_vec(0))
    await _sql(
        "UPDATE memories SET deleted_at = now() WHERE id = CAST(:id AS uuid)",
        id=ids["deleted"],
    )
    ids["old"] = await _write(
        sc, tenant, "refund window was 14 days", embedding=_vec(0, 0.2)
    )
    ids["new"] = await _write(
        sc, tenant, "refund window is 30 days", embedding=_vec(0, 0.21)
    )
    await _sql(
        "UPDATE memories SET status = 'superseded' WHERE id = CAST(:id AS uuid)",
        id=ids["old"],
    )
    await _sql(
        "UPDATE memories SET supersedes_id = CAST(:old AS uuid) WHERE id = CAST(:new AS uuid)",
        old=ids["old"],
        new=ids["new"],
    )
    ids["fleet"] = await _write(
        sc, tenant, "fleet a row", fleet_id="fleet-a", embedding=_vec(5)
    )
    ids["unembedded"] = await _write(sc, tenant, "x" * 500)
    # One entity linked from the first billing row.
    ids["entity"] = str(uuid4())
    await _sql(
        "INSERT INTO entities (id, tenant_id, entity_type, canonical_name) "
        "VALUES (CAST(:eid AS uuid), :t, 'topic', 'Billing')",
        eid=ids["entity"],
        t=tenant,
    )
    await _sql(
        "INSERT INTO memory_entity_links (memory_id, entity_id, role) "
        "VALUES (CAST(:mid AS uuid), CAST(:eid AS uuid), 'mentions')",
        mid=ids["topic"][0],
        eid=ids["entity"],
    )
    return ids


async def _field(client, tenant: str, **params):
    return await client.get(
        "/api/v1/memories/field", params={"tenant_id": tenant, **params}
    )


async def test_field_matches_the_list_for_a_tenant_credential(client, sc, as_auth):
    ids = await _seed(sc)
    tenant = ids["tenant"]
    as_auth(tenant)

    resp = await _field(client, tenant)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    listed = await client.get(
        "/api/v1/memories", params={"tenant_id": tenant, "limit": 100}
    )
    assert listed.status_code == 200, listed.text

    point_ids = {p["id"] for p in body["points"]}
    assert point_ids == {m["id"] for m in listed.json()["items"]}
    assert ids["deleted"] not in point_ids
    assert ids["own_private"] not in point_ids and ids["peer_private"] not in point_ids
    assert ids["old"] in point_ids  # superseded rows are drawn as ghosts
    assert body["total"] == len(point_ids)

    assert body["kind"] == "pca2"
    assert len(body["model_version"]) == 12
    assert body["computed_at"]
    pts = {p["id"]: p for p in body["points"]}
    assert pts[ids["new"]]["supersedes_id"] == ids["old"]
    assert pts[ids["old"]]["superseded_by"] == ids["new"]
    assert pts[ids["old"]]["status"] == "superseded"
    assert pts[ids["unembedded"]]["cluster"] == -1
    assert pts[ids["unembedded"]]["content_chars"] == 500
    assert len(pts[ids["unembedded"]]["preview"]) == 240
    for p in body["points"]:
        assert 0.0 <= p["x"] <= 1.0 and 0.0 <= p["y"] <= 1.0
    assert len(body["clusters"]) >= 2
    assert sum(c["size"] for c in body["clusters"]) == len(point_ids) - 1
    assert pts[ids["topic"][0]]["entity_ids"] == [ids["entity"]]
    assert body["entities"] == [
        {"id": ids["entity"], "label": "Billing", "type": "topic"}
    ]
    # The two topics land in different clusters.
    assert pts[ids["topic"][0]]["cluster"] != pts[ids["topic"][6]]["cluster"]


async def test_agent_sees_its_own_private_row_not_a_peers(client, sc, as_auth):
    ids = await _seed(sc)
    as_auth(ids["tenant"], agent_id=OWNER)
    resp = await _field(client, ids["tenant"])
    assert resp.status_code == 200, resp.text
    point_ids = {p["id"] for p in resp.json()["points"]}
    assert ids["own_private"] in point_ids
    assert ids["peer_private"] not in point_ids


async def test_person_sees_every_scope(client, sc, as_auth):
    ids = await _seed(sc)
    as_auth(ids["tenant"], person=True)
    resp = await _field(client, ids["tenant"])
    assert resp.status_code == 200, resp.text
    point_ids = {p["id"] for p in resp.json()["points"]}
    assert {ids["own_private"], ids["peer_private"]} <= point_ids


async def test_fleet_filter_and_limit(client, sc, as_auth):
    ids = await _seed(sc)
    as_auth(ids["tenant"])
    resp = await _field(client, ids["tenant"], fleet_id="fleet-a")
    assert resp.status_code == 200, resp.text
    assert [p["id"] for p in resp.json()["points"]] == [ids["fleet"]]

    limited = await _field(client, ids["tenant"], limit=3)
    assert limited.status_code == 200, limited.text
    body = limited.json()
    assert len(body["points"]) == 3
    assert body["total"] > 3

    assert (await _field(client, ids["tenant"], limit=50001)).status_code == 422


async def test_tenant_is_required_and_checked(client, sc, as_auth):
    tenant = new_tenant_id()
    as_auth(tenant)
    missing = await client.get("/api/v1/memories/field")
    assert missing.status_code in (403, 422)
    assert (await _field(client, new_tenant_id())).status_code == 403


async def test_empty_tenant(client, as_auth):
    tenant = new_tenant_id()
    as_auth(tenant)
    resp = await _field(client, tenant)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["points"] == [] and body["clusters"] == [] and body["total"] == 0


async def test_cached_until_the_corpus_changes(client, sc, as_auth):
    ids = await _seed(sc)
    tenant = ids["tenant"]
    as_auth(tenant)
    first = (await _field(client, tenant)).json()
    second = (await _field(client, tenant)).json()
    assert second["computed_at"] == first["computed_at"]
    await _write(sc, tenant, "one more", embedding=_vec(0, 0.5))
    third = (await _field(client, tenant)).json()
    assert third["computed_at"] != first["computed_at"]
    assert len(third["points"]) == len(first["points"]) + 1


async def test_storage_page_walks_by_cursor(sc):
    ids = await _seed(sc)
    scope = {"tenant_id": ids["tenant"]}
    seen: list[str] = []
    cursor = None
    while True:
        body = {**scope, "limit": 4}
        if cursor:
            body |= {"cursor_ts": cursor["ts"], "cursor_id": cursor["id"]}
        page = await sc.memory_field_page(body)
        seen.extend(r["id"] for r in page["rows"])
        assert all(len(r["entity_ids"]) <= 1 for r in page["rows"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    sig = await sc.memory_field_signature(scope)
    assert len(seen) == len(set(seen)) == sig["count"]
    assert sig["embedded"] == sig["count"] - 1


async def test_storage_field_routes_require_tenant(storage_http):
    for path in ("field-page", "field-signature"):
        resp = await storage_http.post(f"/api/v1/storage/memories/{path}", json={})
        assert resp.status_code == 422, path
    resp = await storage_http.post(
        "/api/v1/storage/memories/field-page",
        json={"tenant_id": "t", "cursor_ts": "2026-10-01T00:00:00"},
    )
    assert resp.status_code == 422
