"""Saved field layouts: one fitted model per tenant, positions per memory.

core-api laid the field out again in every process on every cold read, paging
every embedding to do it. Storage now keeps what a fit produces, so one process
fits a tenant (under a claim) and readers serve saved positions, embedding only
the memories that have none for the current model.
"""

from __future__ import annotations

import base64
import json
import uuid

from httpx import AsyncClient
from sqlalchemy import text

from core_storage_api.services.postgres_service import get_session
from tests.test_integration import PREFIX, _memory_payload, fake_embedding

PARAMS = base64.b64encode(b"\x00\x01fitted-model-bytes\xfe\xff").decode()


def _tenant() -> str:
    return f"proj-{uuid.uuid4().hex[:10]}"


async def _memory(client: AsyncClient, tenant_id: str) -> str:
    response = await client.post(f"{PREFIX}/memories", json=_memory_payload(tenant_id, "proj-fleet"))
    assert response.status_code == 200, response.text
    return response.json()["id"]


async def _post(client: AsyncClient, path: str, body: dict) -> dict:
    response = await client.post(f"{PREFIX}{path}", json=body)
    assert response.status_code == 200, response.text
    return response.json()


async def _claim(client: AsyncClient, tenant_id: str, token: str, **extra: object) -> dict:
    return await _post(client, "/projections/claim", {"tenant_id": tenant_id, "claim_token": token, **extra})


async def _release(client: AsyncClient, tenant_id: str, token: str) -> bool:
    body = {"tenant_id": tenant_id, "claim_token": token}
    return (await _post(client, "/projections/release", body))["released"]


def _model(tenant_id: str, token: str, version: str) -> dict:
    return {
        "tenant_id": tenant_id,
        "claim_token": token,
        "model_version": version,
        "kind": "pca2",
        "dim": 4,
        "n_fit": 3,
        "params_b64": PARAMS,
    }


async def _get_model(client: AsyncClient, tenant_id: str) -> dict:
    response = await client.get(f"{PREFIX}/projections/model/{tenant_id}")
    assert response.status_code == 200, response.text
    return response.json()


async def _place(
    client: AsyncClient, tenant_id: str, version: str, points: list[tuple[str, float, float, int]]
) -> int:
    """Save positions the way a fit does: under its claim, each naming the row
    version it read the memory at."""
    rows = await _rows(client, tenant_id, model_version=version)
    token = f"placer-{uuid.uuid4().hex[:8]}"
    assert (await _claim(client, tenant_id, token))["claimed"] is True
    items = [
        {"memory_id": m, "x": x, "y": y, "cluster": c, "row_version": _row_version(rows.get(m))}
        for m, x, y, c in points
    ]
    body = {"tenant_id": tenant_id, "model_version": version, "claim_token": token, "items": items}
    saved = (await _post(client, "/projections/positions", body))["saved"]
    assert await _release(client, tenant_id, token) is True
    return saved


def _row_version(row: dict | None) -> str:
    # Another tenant's memory has no row here; any version will do, since it
    # is skipped as foreign.
    return (row or {}).get("row_version") or "0"


async def _rows(client: AsyncClient, tenant_id: str, **extra: object) -> dict[str, dict]:
    page = await _post(client, "/memories/field-page", {"tenant_id": tenant_id, **extra})
    return {r["id"]: r for r in page["rows"]}


async def test_one_claimant_fits_a_tenant_at_a_time(client: AsyncClient) -> None:
    tenant = _tenant()
    first = await _claim(client, tenant, "a")
    assert first["claimed"] is True
    assert first["model_version"] is None
    assert (await _claim(client, tenant, "b"))["claimed"] is False
    # The holder re-sending its own claim (a retried request) still holds it.
    assert (await _claim(client, tenant, "a"))["claimed"] is True
    assert (await _get_model(client, tenant))["fitting"] is True

    assert await _release(client, tenant, "b") is False
    assert await _release(client, tenant, "a") is True
    assert (await _get_model(client, tenant))["fitting"] is False
    assert (await _claim(client, tenant, "b"))["claimed"] is True


async def test_a_claim_past_its_lease_can_be_taken(client: AsyncClient) -> None:
    """A process that died mid-fit leaves its claim behind; once the lease it
    asked for has run out, another process may fit the tenant."""
    tenant = _tenant()
    assert (await _claim(client, tenant, "a", lease_seconds=60))["claimed"] is True
    assert (await _claim(client, tenant, "b", lease_seconds=60))["claimed"] is False
    async with get_session() as session:
        await session.execute(
            text(
                "UPDATE projection_models SET claim_expires_at = now() - interval '1 second' WHERE tenant_id = :t"
            ),
            {"t": tenant},
        )
    assert (await _claim(client, tenant, "b", lease_seconds=60))["claimed"] is True


async def test_claims_are_validated(client: AsyncClient) -> None:
    tenant = _tenant()
    for body in (
        {"tenant_id": tenant},
        {"tenant_id": tenant, "claim_token": ""},
        {"tenant_id": tenant, "claim_token": "a", "lease_seconds": 5},
        {"tenant_id": tenant, "claim_token": "a", "lease_seconds": 7200},
        {"claim_token": "a"},
    ):
        response = await client.post(f"{PREFIX}/projections/claim", json=body)
        assert response.status_code == 422, body


async def test_a_tenant_never_fitted_has_no_model(client: AsyncClient) -> None:
    model = await _get_model(client, _tenant())
    assert model["model_version"] is None
    assert model["params_b64"] is None
    assert model["fitting"] is False


async def test_only_the_claim_holder_saves_a_model_and_saving_releases_the_claim(client: AsyncClient) -> None:
    tenant = _tenant()
    unclaimed = await client.put(f"{PREFIX}/projections/model", json=_model(tenant, "a", "v1"))
    assert unclaimed.status_code == 409, unclaimed.text

    await _claim(client, tenant, "a")
    lost = await client.put(f"{PREFIX}/projections/model", json=_model(tenant, "b", "v1"))
    assert lost.status_code == 409, lost.text
    saved = await client.put(f"{PREFIX}/projections/model", json=_model(tenant, "a", "v1"))
    assert saved.status_code == 200, saved.text

    model = await _get_model(client, tenant)
    assert model["model_version"] == "v1"
    assert model["params_b64"] == PARAMS
    assert (model["kind"], model["dim"], model["n_fit"]) == ("pca2", 4, 3)
    assert model["fitted_at"]
    assert model["fitting"] is False

    # Saving released the claim, so the old holder's next save is refused and
    # the next fit can be claimed by anyone.
    again = await client.put(f"{PREFIX}/projections/model", json=_model(tenant, "a", "v2"))
    assert again.status_code == 409, again.text
    claim = await _claim(client, tenant, "b")
    assert claim["claimed"] is True
    assert claim["model_version"] == "v1"


async def test_models_are_validated(client: AsyncClient) -> None:
    tenant = _tenant()
    await _claim(client, tenant, "a")
    for patch in (
        {"model_version": ""},
        {"params_b64": "not base64!"},
        {"params_b64": None},
        {"dim": 0},
        {"n_fit": -1},
        {"kind": ""},
    ):
        response = await client.put(
            f"{PREFIX}/projections/model", json={**_model(tenant, "a", "v1"), **patch}
        )
        assert response.status_code == 422, patch
    assert (await _get_model(client, tenant))["model_version"] is None


async def test_positions_are_saved_only_for_the_tenants_own_memories(client: AsyncClient) -> None:
    tenant, other = _tenant(), _tenant()
    mine = await _memory(client, tenant)
    theirs = await _memory(client, other)
    saved = await _place(client, tenant, "v1", [(mine, 0.25, 0.75, 2), (theirs, 0.5, 0.5, 0)])
    assert saved == 1

    row = (await _rows(client, tenant, model_version="v1"))[mine]
    assert (row["x"], row["y"], row["cluster"]) == (0.25, 0.75, 2)
    assert (await _rows(client, other, model_version="v1"))[theirs]["x"] is None

    # Placing again replaces the position.
    assert await _place(client, tenant, "v1", [(mine, 0.125, 0.5, 1)]) == 1
    row = (await _rows(client, tenant, model_version="v1"))[mine]
    assert (row["x"], row["y"], row["cluster"]) == (0.125, 0.5, 1)


async def test_positions_are_validated(client: AsyncClient) -> None:
    tenant = _tenant()
    memory = await _memory(client, tenant)
    good = {"memory_id": memory, "x": 0.5, "y": 0.5, "cluster": 0, "row_version": "1"}
    no_version = {k: v for k, v in good.items() if k != "row_version"}
    for body in (
        {"tenant_id": tenant, "model_version": "v1", "items": [no_version]},
        {"tenant_id": tenant, "model_version": "v1", "items": [{**good, "row_version": "abc"}]},
        {"tenant_id": tenant, "model_version": "v1", "items": [{**good, "row_version": 1}]},
        {"tenant_id": tenant, "model_version": "v1", "items": []},
        {"tenant_id": tenant, "model_version": "v1", "items": [good] * 2001},
        {"tenant_id": tenant, "items": [good]},
        {"tenant_id": tenant, "model_version": "v1", "items": [{**good, "x": float("nan")}]},
        # Too large for a float, and too large for the REAL column.
        {"tenant_id": tenant, "model_version": "v1", "items": [{**good, "x": 10**400}]},
        {"tenant_id": tenant, "model_version": "v1", "items": [{**good, "y": 1e39}]},
        # Outside the [0, 1] the layout normalizes to.
        {"tenant_id": tenant, "model_version": "v1", "items": [{**good, "x": 1.5}]},
        {"tenant_id": tenant, "model_version": "v1", "items": [{**good, "y": -0.1}]},
        {"tenant_id": tenant, "model_version": "v1", "items": [{**good, "y": "0.5"}]},
        {"tenant_id": tenant, "model_version": "v1", "items": [{**good, "cluster": -1}]},
        {"tenant_id": tenant, "model_version": "v1", "items": [{**good, "cluster": 40000}]},
        {"tenant_id": tenant, "model_version": "v1", "items": [{**good, "memory_id": "not-a-uuid"}]},
    ):
        # Encoded here rather than by httpx, which refuses NaN; the server's
        # ``json.loads`` accepts it, so the route has to.
        response = await client.post(
            f"{PREFIX}/projections/positions",
            content=json.dumps(body),
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 422, str(body)[:200]
    assert (await _rows(client, tenant, model_version="v1"))[memory]["x"] is None


async def test_the_field_page_serves_saved_positions_and_embeds_only_the_unplaced(
    client: AsyncClient,
) -> None:
    tenant = _tenant()
    placed = await _memory(client, tenant)
    unplaced = await _memory(client, tenant)
    await _place(client, tenant, "v1", [(placed, 0.25, 0.5, 3)])

    rows = await _rows(client, tenant, model_version="v1", embeddings="unplaced")
    assert (rows[placed]["x"], rows[placed]["y"], rows[placed]["cluster"]) == (0.25, 0.5, 3)
    assert rows[placed]["embedding_b64"] is None
    assert rows[unplaced]["x"] is None
    assert rows[unplaced]["embedding_b64"]

    # A position saved for another model version does not place the memory.
    rows = await _rows(client, tenant, model_version="v2", embeddings="unplaced")
    assert rows[placed]["x"] is None
    assert rows[placed]["embedding_b64"]

    rows = await _rows(client, tenant, model_version="v1", embeddings="none")
    assert rows[placed]["x"] == 0.25
    assert rows[unplaced]["embedding_b64"] is None

    bad = await client.post(
        f"{PREFIX}/memories/field-page",
        json={"tenant_id": tenant, "model_version": "v1", "embeddings": "some"},
    )
    assert bad.status_code == 422, bad.text


async def test_the_field_page_is_unchanged_for_callers_that_ask_for_no_positions(client: AsyncClient) -> None:
    """The current core-api reads pages without either field; a rolling
    deploy must not change what it gets."""
    tenant = _tenant()
    memory = await _memory(client, tenant)
    await _place(client, tenant, "v1", [(memory, 0.25, 0.5, 0)])
    row = (await _rows(client, tenant))[memory]
    assert row["embedding_b64"]
    assert "x" not in row
    assert "cluster" not in row
    assert "row_version" not in row


async def test_a_new_embedding_clears_the_memorys_saved_position(client: AsyncClient) -> None:
    tenant = _tenant()
    moved = await _memory(client, tenant)
    patched = await _memory(client, tenant)
    kept = await _memory(client, tenant)
    await _place(
        client, tenant, "v1", [(moved, 0.25, 0.25, 0), (patched, 0.5, 0.5, 1), (kept, 0.75, 0.75, 2)]
    )

    response = await client.patch(
        f"{PREFIX}/memories/{moved}/embedding",
        json={"tenant_id": tenant, "embedding": fake_embedding("moved")},
    )
    assert response.status_code == 200, response.text
    response = await client.patch(
        f"{PREFIX}/memories/{patched}", json={"tenant_id": tenant, "embedding": fake_embedding("patched")}
    )
    assert response.status_code == 200, response.text
    # An edit that leaves the embedding alone leaves the position alone.
    response = await client.patch(f"{PREFIX}/memories/{kept}", json={"tenant_id": tenant, "title": "renamed"})
    assert response.status_code == 200, response.text

    rows = await _rows(client, tenant, model_version="v1", embeddings="unplaced")
    assert rows[moved]["x"] is None
    assert rows[moved]["embedding_b64"]
    assert rows[patched]["x"] is None
    assert rows[kept]["x"] == 0.75


async def test_saving_a_model_drops_positions_from_other_versions(client: AsyncClient) -> None:
    tenant = _tenant()
    memory = await _memory(client, tenant)
    await _place(client, tenant, "v1", [(memory, 0.25, 0.25, 0)])
    await _place(client, tenant, "v2", [(memory, 0.75, 0.75, 1)])
    await _claim(client, tenant, "a")
    saved = await client.put(f"{PREFIX}/projections/model", json=_model(tenant, "a", "v2"))
    assert saved.status_code == 200, saved.text

    assert (await _rows(client, tenant, model_version="v1"))[memory]["x"] is None
    assert (await _rows(client, tenant, model_version="v2"))[memory]["x"] == 0.75


async def test_a_position_computed_from_an_old_embedding_is_not_saved(client: AsyncClient) -> None:
    """A fit reads a memory, and its embedding changes before the fit saves.
    The position describes the old vector, so it is skipped: the memory stays
    unplaced, and its new embedding goes to the next reader to place."""
    tenant = _tenant()
    memory = await _memory(client, tenant)
    read = (await _rows(client, tenant, model_version="v1"))[memory]
    response = await client.patch(
        f"{PREFIX}/memories/{memory}/embedding",
        json={"tenant_id": tenant, "embedding": fake_embedding("re-embedded")},
    )
    assert response.status_code == 200, response.text

    await _claim(client, tenant, "fit")
    item = {"memory_id": memory, "x": 0.25, "y": 0.25, "cluster": 0, "row_version": _row_version(read)}
    body = {"tenant_id": tenant, "model_version": "v1", "claim_token": "fit", "items": [item]}
    assert (await _post(client, "/projections/positions", body))["saved"] == 0
    row = (await _rows(client, tenant, model_version="v1", embeddings="unplaced"))[memory]
    assert row["x"] is None
    assert row["embedding_b64"]


async def test_positions_need_the_saved_model_version_or_the_live_claim(client: AsyncClient) -> None:
    """A late write from a fit that lost its claim, or under a retired version,
    would leave rows nothing reads until the next fit."""
    tenant = _tenant()
    memory = await _memory(client, tenant)

    async def save(version: str, token: str | None = None) -> int:
        row = (await _rows(client, tenant, model_version=version))[memory]
        item = {"memory_id": memory, "x": 0.5, "y": 0.5, "cluster": 0, "row_version": _row_version(row)}
        body: dict = {"tenant_id": tenant, "model_version": version, "items": [item]}
        if token:
            body["claim_token"] = token
        return (await client.post(f"{PREFIX}/projections/positions", json=body)).status_code

    assert await save("v1") == 409  # never fitted, and no claim
    await _claim(client, tenant, "fit")
    assert await save("v1", "other") == 409
    assert await save("v1", "fit") == 200  # the fit writing the version it is fitting
    saved = await client.put(f"{PREFIX}/projections/model", json=_model(tenant, "fit", "v1"))
    assert saved.status_code == 200, saved.text
    assert await save("v1") == 200  # a reader placing a new memory under the saved model
    assert await save("v0") == 409  # a retired or unknown version
    assert await save("v0", "fit") == 409  # the claim ended with the save
