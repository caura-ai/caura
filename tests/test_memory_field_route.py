"""``GET /api/v1/memories/field`` end to end, through the in-process storage bridge.

The field must place exactly the rows ``GET /memories`` would list to the same
caller: same tenant check, same visibility scoping (``scope_agent`` rows only
for their author, every scope for a person), same strict fleet filter, deleted
rows out, superseded rows in. Plus the shape the map frontend reads, and the
saved layout it is served from (the section at the end).
"""

from __future__ import annotations

import asyncio
import contextlib
from uuid import uuid4

import pytest
from fastapi import Request
from sqlalchemy import text

from core_api.app import app
from core_api.auth import AuthContext, get_auth_context
from core_api.constants import VECTOR_DIM
from core_api.services import memory_field
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

    memory_field._reset_state()
    yield _install
    app.dependency_overrides.pop(get_auth_context, None)
    # A fit or read a failed test left behind must not leak into the next one.
    memory_field._reset_state()


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
    memory_field._signature_cache.clear()  # the five-second signature TTL has elapsed
    third = (await _field(client, tenant)).json()
    assert third["computed_at"] != first["computed_at"]
    assert len(third["points"]) == len(first["points"]) + 1


async def test_an_edit_misses_the_cache(client, sc, as_auth):
    """An edit keeps the row count, the embedded count and the newest
    timestamp, so the cache must notice the edited columns themselves: a
    redacted memory may not keep showing its old preview."""
    ids = await _seed(sc)
    tenant = ids["tenant"]
    as_auth(tenant)
    first = (await _field(client, tenant)).json()
    edited = ids["topic"][1]
    await _sql(
        "UPDATE memories SET content = 'refund note, redacted', "
        "content_hash = md5('refund note, redacted'), visibility = 'scope_org' "
        "WHERE id = CAST(:id AS uuid)",
        id=edited,
    )
    memory_field._signature_cache.clear()  # the five-second signature TTL has elapsed
    second = (await _field(client, tenant)).json()
    assert second["computed_at"] != first["computed_at"]
    point = next(p for p in second["points"] if p["id"] == edited)
    assert point["preview"] == "refund note, redacted"
    assert point["visibility"] == "scope_org"


class _HeldReads:
    """The storage client, except that each map read's first page waits.

    A read of a saved map starts by fetching its first page (no cursor) with
    only the unplaced rows' embeddings; that fetch counts the read and then
    holds until ``release`` is set, so a test can line up requests against
    reads in flight. A fit's pages carry every embedding and are not held.
    """

    def __init__(self, real) -> None:
        self._real = real
        self.release = asyncio.Event()
        self.reads = 0
        self._started = asyncio.Condition()

    def __getattr__(self, name: str):
        return getattr(self._real, name)

    async def memory_field_page(self, body: dict) -> dict:
        if "cursor_id" not in body and body.get("embeddings") == "unplaced":
            async with self._started:
                self.reads += 1
                self._started.notify_all()
            await self.release.wait()
        return await self._real.memory_field_page(body)

    async def until_reads(self, count: int) -> None:
        async with self._started:
            await self._started.wait_for(lambda: self.reads >= count)


@pytest.fixture
def held_reads(sc, monkeypatch):
    held = _HeldReads(memory_field.get_storage_client())
    monkeypatch.setattr(memory_field, "get_storage_client", lambda: held)
    yield held
    held.release.set()


async def test_requests_for_one_map_share_one_read(client, sc, as_auth, held_reads):
    tenant = (await _seed(sc))["tenant"]
    as_auth(tenant)
    first = asyncio.create_task(_field(client, tenant))
    async with asyncio.timeout(5):
        await held_reads.until_reads(1)
    second = asyncio.create_task(_field(client, tenant))
    # Unshared, the second request starts a read of its own.
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(2):
            await held_reads.until_reads(2)
    held_reads.release.set()
    one, two = await first, await second
    assert one.status_code == two.status_code == 200
    assert one.json()["computed_at"] == two.json()["computed_at"]
    assert held_reads.reads == 1


async def test_every_limit_serves_the_same_saved_positions(client, sc, as_auth):
    tenant = (await _seed(sc))["tenant"]
    as_auth(tenant)
    full = {p["id"]: p for p in (await _field(client, tenant)).json()["points"]}
    few = (await _field(client, tenant, limit=5)).json()["points"]
    assert len(few) == 5
    for p in few:
        assert (p["x"], p["y"], p["cluster"]) == (
            full[p["id"]]["x"],
            full[p["id"]]["y"],
            full[p["id"]]["cluster"],
        )


async def test_maps_of_different_tenants_do_not_wait_on_each_other(
    client, sc, as_auth, held_reads
):
    tenants = [(await _seed(sc))["tenant"] for _ in range(3)]

    async def _each_its_own_tenant(request: Request) -> AuthContext:
        tenant = request.query_params["tenant_id"]
        set_current_tenant(tenant)
        return AuthContext(tenant_id=tenant, readable_tenant_ids=[tenant])

    app.dependency_overrides[get_auth_context] = _each_its_own_tenant
    running = [asyncio.create_task(_field(client, t)) for t in tenants]
    async with asyncio.timeout(10):
        await held_reads.until_reads(3)
    held_reads.release.set()
    assert [(await r).status_code for r in running] == [200, 200, 200]


async def test_concurrent_requests_share_one_signature_scan(
    client, sc, as_auth, monkeypatch
):
    """The signature scans every visible row, cache hit or not, so a burst of
    requests for one scope runs it once."""
    tenant = (await _seed(sc))["tenant"]
    as_auth(tenant)
    real = memory_field.get_storage_client()
    release = asyncio.Event()
    started = asyncio.Condition()
    scans = 0

    class _HeldSignatures:
        def __getattr__(self, name: str):
            return getattr(real, name)

        async def memory_field_signature(self, scope: dict) -> dict:
            nonlocal scans
            async with started:
                scans += 1
                started.notify_all()
            await release.wait()
            return await real.memory_field_signature(scope)

    held = _HeldSignatures()
    monkeypatch.setattr(memory_field, "get_storage_client", lambda: held)

    async def until_scans(count: int) -> None:
        async with started:
            await started.wait_for(lambda: scans >= count)

    first = asyncio.create_task(_field(client, tenant))
    async with asyncio.timeout(5):
        await until_scans(1)
    rest = [asyncio.create_task(_field(client, tenant)) for _ in range(2)]
    # Unshared, each of the others starts a scan of its own.
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(2):
            await until_scans(2)
    release.set()
    responses = [await first] + [await r for r in rest]
    assert [r.status_code for r in responses] == [200, 200, 200]
    assert scans == 1


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


# ---------------------------------------------------------------------------
# Saved layouts: one process fits a tenant under a storage claim and saves
# the model and the positions; a read serves saved positions and places only
# the memories that have none, with the saved model, without refitting.
# ---------------------------------------------------------------------------

# The seed's shared, live, embedded rows: two topics of six, the superseded
# pair and the fleet row. Private, deleted and unembedded rows are not fitted.
SHARED_EMBEDDED = 15


async def _saved_positions(sc, tenant: str, version: str) -> dict[str, tuple]:
    page = await sc.memory_field_page(
        {
            "tenant_id": tenant,
            "include_scope_agent": True,
            "model_version": version,
            "embeddings": "none",
        }
    )
    return {
        r["id"]: (r["x"], r["y"], r["cluster"])
        for r in page["rows"]
        if r["x"] is not None
    }


async def test_the_first_request_fits_and_saves_the_layout(client, sc, as_auth):
    ids = await _seed(sc)
    tenant = ids["tenant"]
    as_auth(tenant)
    resp = await _field(client, tenant)
    assert resp.status_code == 200, resp.text

    model = await sc.projection_model_get(tenant)
    assert model["model_version"]
    assert model["fitting"] is False
    assert model["n_fit"] == SHARED_EMBEDDED
    saved = await _saved_positions(sc, tenant, model["model_version"])
    assert set(saved) == {
        p["id"] for p in resp.json()["points"] if p["cluster"] >= 0
    }, "every placed point is a saved position"
    for p in resp.json()["points"]:
        if p["id"] in saved:
            x, y, cluster = saved[p["id"]]
            assert (p["x"], p["y"], p["cluster"]) == (round(x, 5), round(y, 5), cluster)


async def test_a_new_memory_is_placed_without_a_refit(client, sc, as_auth):
    ids = await _seed(sc)
    tenant = ids["tenant"]
    as_auth(tenant)
    first = {p["id"]: p for p in (await _field(client, tenant)).json()["points"]}
    version = (await sc.projection_model_get(tenant))["model_version"]

    added = await _write(
        sc, tenant, "billing refund note, one more", embedding=_vec(0, 0.03)
    )
    memory_field._signature_cache.clear()  # the five-second signature TTL has elapsed
    second = {p["id"]: p for p in (await _field(client, tenant)).json()["points"]}

    assert (await sc.projection_model_get(tenant))["model_version"] == version
    assert second[added]["cluster"] == second[ids["topic"][0]]["cluster"]
    assert added in await _saved_positions(sc, tenant, version)
    for memory_id, point in first.items():
        assert (second[memory_id]["x"], second[memory_id]["y"]) == (
            point["x"],
            point["y"],
        ), memory_id


async def test_private_memories_are_placed_but_do_not_shape_the_layout(
    client, sc, as_auth
):
    ids = await _seed(sc)
    tenant = ids["tenant"]
    as_auth(tenant)
    assert (await _field(client, tenant)).status_code == 200
    model = await sc.projection_model_get(tenant)
    assert model["n_fit"] == SHARED_EMBEDDED

    memory_field._reset_state()
    as_auth(tenant, agent_id=OWNER)
    points = {p["id"]: p for p in (await _field(client, tenant)).json()["points"]}
    assert points[ids["own_private"]]["cluster"] >= 0
    assert ids["own_private"] in await _saved_positions(
        sc, tenant, model["model_version"]
    )
    assert (await sc.projection_model_get(tenant))["model_version"] == model[
        "model_version"
    ]


async def test_a_large_tenant_is_prepared_in_the_background(
    client, sc, as_auth, monkeypatch
):
    ids = await _seed(sc)
    tenant = ids["tenant"]
    as_auth(tenant)
    monkeypatch.setattr(memory_field, "FIT_INLINE_MAX", 0)
    preparing = await _field(client, tenant)
    assert preparing.status_code == 429, preparing.text
    assert "prepared" in preparing.json()["detail"]
    assert preparing.headers["Retry-After"]

    async with asyncio.timeout(30):
        await asyncio.gather(*memory_field._fits.values())
    served = await _field(client, tenant)
    assert served.status_code == 200, served.text
    assert (await sc.projection_model_get(tenant))["model_version"]


async def test_a_failed_fit_gives_its_claim_back(client, sc, as_auth, monkeypatch):
    ids = await _seed(sc)
    tenant = ids["tenant"]
    as_auth(tenant)

    def broken(vectors):
        raise RuntimeError("fit failed")

    real = memory_field.fit_model
    monkeypatch.setattr(memory_field, "fit_model", broken)
    failed = await _field(client, tenant)
    assert failed.status_code == 429, failed.text
    model = await sc.projection_model_get(tenant)
    assert model["fitting"] is False
    assert model["model_version"] is None

    monkeypatch.setattr(memory_field, "fit_model", real)
    memory_field._reset_state()
    assert (await _field(client, tenant)).status_code == 200
